#include "proxy_parse.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netdb.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <sys/wait.h>
#include <errno.h>
#include <pthread.h>
#include <semaphore.h>

#define MAX_BYTES 4096    //max allowed size of request/response
#define MAX_CLIENTS 400     //max number of client requests served at a time
#define MAX_SIZE 200*(1<<20)     //size of the cache
#define MAX_ELEMENT_SIZE 10*(1<<20)     //max size of an element in cache
#define HASH_TABLE_SIZE 1024    //number of buckets in the hash table

typedef struct cache_element cache_element;

struct cache_element {
    char* data;
    int len;
    char* url;
    cache_element* next;
    cache_element* hash_next;
    cache_element* lru_prev;
    cache_element* lru_next;
};

cache_element* find(char* url);
int add_cache_element(char* data,int size,char* url);
void remove_cache_element();
static void _remove_cache_element_unlocked();  // internal: caller must hold lock

#define MAX_PENDING 1024

typedef struct pending_entry {
    char url[512];
    pthread_cond_t cond;
    pthread_mutex_t mutex;
    int ref_count;
    int completed;
    struct pending_entry* next;
} pending_entry_t;

pending_entry_t* pending_table[MAX_PENDING];
pthread_mutex_t pending_lock;

unsigned int pending_hash(const char* url) {
    unsigned int hash = 2166136261U;
    while (*url) {
        hash ^= (unsigned char)*url;
        hash *= 16777619U;
        url++;
    }
    return hash % MAX_PENDING;
}

pending_entry_t* find_pending(const char* url) {
    unsigned int index = pending_hash(url);
    pending_entry_t* entry = pending_table[index];
    while (entry != NULL) {
        if (!strcmp(entry->url, url)) {
            return entry;
        }
        entry = entry->next;
    }
    return NULL;
}

void add_pending(pending_entry_t* entry) {
    unsigned int index = pending_hash(entry->url);
    entry->next = pending_table[index];
    pending_table[index] = entry;
}

pending_entry_t* create_pending_entry(const char* url) {
    pending_entry_t* entry = (pending_entry_t*)malloc(sizeof(pending_entry_t));
    strncpy(entry->url, url, sizeof(entry->url) - 1);
    entry->url[sizeof(entry->url) - 1] = '\0';
    pthread_cond_init(&entry->cond, NULL);
    pthread_mutex_init(&entry->mutex, NULL);
    entry->ref_count = 1;
    entry->completed = 0;
    entry->next = NULL;
    return entry;
}

pending_entry_t* pending_wait_or_register(const char* url) {
    pending_entry_t* entry;
    pthread_mutex_lock(&pending_lock);
    entry = find_pending(url);
    if (entry == NULL) {
        entry = create_pending_entry(url);
        add_pending(entry);
        pthread_mutex_unlock(&pending_lock);
        return NULL;
    } else {
        entry->ref_count++;
        pthread_mutex_unlock(&pending_lock);
        return entry;
    }
}

void pending_wait(pending_entry_t* entry) {
    pthread_mutex_lock(&entry->mutex);
    while (!entry->completed) {
        pthread_cond_wait(&entry->cond, &entry->mutex);
    }
    pthread_mutex_unlock(&entry->mutex);

    pthread_mutex_lock(&pending_lock);
    entry->ref_count--;
    if (entry->ref_count == 0) {
        pthread_cond_destroy(&entry->cond);
        pthread_mutex_destroy(&entry->mutex);
        free(entry);
    }
    pthread_mutex_unlock(&pending_lock);
}

void pending_complete(const char* url) {
    pending_entry_t* entry;
    pthread_mutex_lock(&pending_lock);
    
    unsigned int index = pending_hash(url);
    entry = pending_table[index];
    pending_entry_t* prev = NULL;
    while (entry != NULL) {
        if (!strcmp(entry->url, url)) {
            if (prev == NULL) {
                pending_table[index] = entry->next;
            } else {
                prev->next = entry->next;
            }
            break;
        }
        prev = entry;
        entry = entry->next;
    }
    
    if (entry) {
        pthread_mutex_lock(&entry->mutex);
        entry->completed = 1;
        pthread_cond_broadcast(&entry->cond);
        pthread_mutex_unlock(&entry->mutex);
        
        entry->ref_count--;
        if (entry->ref_count == 0) {
            pthread_cond_destroy(&entry->cond);
            pthread_mutex_destroy(&entry->mutex);
            free(entry);
        }
    }
    pthread_mutex_unlock(&pending_lock);
}


#define MAX_POOL_SIZE 100
#define MAX_HOST_BUCKETS 50

typedef struct pooled_connection {
    char host[256];
    int port;
    int socket_fd;
    time_t last_used;
    struct pooled_connection* next;
} pooled_connection_t;

typedef struct {
    pooled_connection_t* head;
    int count;
    pthread_mutex_t lock;
} connection_pool_t;

connection_pool_t host_pools[MAX_HOST_BUCKETS];
pthread_mutex_t pool_lock;

unsigned int pool_hash(const char* host) {
    unsigned int hash = 2166136261U;
    while (*host) {
        hash ^= (unsigned char)*host;
        hash *= 16777619U;
        host++;
    }
    return hash % MAX_HOST_BUCKETS;
}

void init_connection_pool() {
    pthread_mutex_init(&pool_lock, NULL);
    for (int i = 0; i < MAX_HOST_BUCKETS; i++) {
        host_pools[i].head = NULL;
        host_pools[i].count = 0;
        pthread_mutex_init(&host_pools[i].lock, NULL);
    }
}

int is_connection_alive(int socket_fd) {
    int error = 0;
    socklen_t len = sizeof(error);
    int retval = getsockopt(socket_fd, SOL_SOCKET, SO_ERROR, &error, &len);
    if (retval != 0 || error != 0) {
        return 0;
    }
    
    char buf[1];
    int res = recv(socket_fd, buf, 1, MSG_PEEK | MSG_DONTWAIT);
    if (res == 0) {
        return 0; // Peer closed connection
    }
    if (res < 0 && (errno != EAGAIN && errno != EWOULDBLOCK)) {
        return 0; // Error on connection
    }
    
    return 1;
}

int get_from_pool(const char* host, int port) {
    unsigned int index = pool_hash(host);
    pthread_mutex_lock(&host_pools[index].lock);
    
    pooled_connection_t* curr = host_pools[index].head;
    pooled_connection_t* prev = NULL;
    
    while (curr != NULL) {
        if (!strcmp(curr->host, host) && curr->port == port) {
            int fd = curr->socket_fd;
            if (prev == NULL) {
                host_pools[index].head = curr->next;
            } else {
                prev->next = curr->next;
            }
            host_pools[index].count--;
            free(curr);
            pthread_mutex_unlock(&host_pools[index].lock);
            
            if (is_connection_alive(fd)) {
                printf("Reusing connection from pool for %s:%d\n", host, port);
                return fd;
            } else {
                close(fd);
                return get_from_pool(host, port); // try another one
            }
        }
        prev = curr;
        curr = curr->next;
    }
    
    pthread_mutex_unlock(&host_pools[index].lock);
    return -1;
}

int return_to_pool(const char* host, int port, int socket_fd) {
    if (!is_connection_alive(socket_fd)) {
        close(socket_fd);
        return 0;
    }

    unsigned int index = pool_hash(host);
    pthread_mutex_lock(&host_pools[index].lock);
    
    if (host_pools[index].count >= MAX_POOL_SIZE / MAX_HOST_BUCKETS) { 
        pthread_mutex_unlock(&host_pools[index].lock);
        close(socket_fd);
        return 0;
    }
    
    pooled_connection_t* entry = (pooled_connection_t*)malloc(sizeof(pooled_connection_t));
    if (entry == NULL) {
        pthread_mutex_unlock(&host_pools[index].lock);
        close(socket_fd);
        return 0;
    }
    strncpy(entry->host, host, sizeof(entry->host)-1);
    entry->host[sizeof(entry->host)-1] = '\0';
    entry->port = port;
    entry->socket_fd = socket_fd;
    entry->last_used = time(NULL);
    
    entry->next = host_pools[index].head;
    host_pools[index].head = entry;
    host_pools[index].count++;
    
    pthread_mutex_unlock(&host_pools[index].lock);
    printf("Returned connection to pool for %s:%d\n", host, port);
    return 1;
}

void cleanup_connection_pool() {
    for (int i = 0; i < MAX_HOST_BUCKETS; i++) {
        pthread_mutex_lock(&host_pools[i].lock);
        pooled_connection_t* curr = host_pools[i].head;
        while (curr != NULL) {
            pooled_connection_t* temp = curr;
            curr = curr->next;
            close(temp->socket_fd);
            free(temp);
        }
        host_pools[i].head = NULL;
        host_pools[i].count = 0;
        pthread_mutex_unlock(&host_pools[i].lock);
    }
}

// LRU Helper Functions
void lru_init();
void lru_remove(cache_element* element);
void lru_append_tail(cache_element* element);
cache_element* lru_get_lru();

int port_number = 8080;				// Default Port
int proxy_socketId;					// socket descriptor of proxy server
pthread_t tid[MAX_CLIENTS];         //array to store the thread ids of clients
sem_t seamaphore;	                //if client requests exceeds the max_clients this seamaphore puts the
                                    //waiting threads to sleep and wakes them when traffic on queue decreases
//sem_t cache_lock;			       
pthread_rwlock_t rwlock;               //rwlock is used for locking the cache
pthread_mutex_t lru_mutex;

cache_element* lru_head = NULL;
cache_element* lru_tail = NULL;
int cache_size;             //cache_size denotes the current size of the cache

cache_element* hash_table[HASH_TABLE_SIZE]; //hash table for O(1) cache lookups

// FNV-1a hash function for fast URL hashing
unsigned int fnv1a_hash(const char* url) {
    unsigned int hash = 2166136261U;
    while (*url) {
        hash ^= (unsigned char)*url;
        hash *= 16777619U;
        url++;
    }
    return hash % HASH_TABLE_SIZE;
}

int sendErrorMessage(int socket, int status_code)
{
	char str[1024];
	char currentTime[50];
	time_t now = time(0);

	struct tm data = *gmtime(&now);
	strftime(currentTime,sizeof(currentTime),"%a, %d %b %Y %H:%M:%S %Z", &data);

	switch(status_code)
	{
		case 400: snprintf(str, sizeof(str), "HTTP/1.1 400 Bad Request\r\nContent-Length: 95\r\nConnection: keep-alive\r\nContent-Type: text/html\r\nDate: %s\r\nServer: VaibhavN/14785\r\n\r\n<HTML><HEAD><TITLE>400 Bad Request</TITLE></HEAD>\n<BODY><H1>400 Bad Rqeuest</H1>\n</BODY></HTML>", currentTime);
				  printf("400 Bad Request\n");
				  send(socket, str, strlen(str), 0);
				  break;

		case 403: snprintf(str, sizeof(str), "HTTP/1.1 403 Forbidden\r\nContent-Length: 112\r\nContent-Type: text/html\r\nConnection: keep-alive\r\nDate: %s\r\nServer: VaibhavN/14785\r\n\r\n<HTML><HEAD><TITLE>403 Forbidden</TITLE></HEAD>\n<BODY><H1>403 Forbidden</H1><br>Permission Denied\n</BODY></HTML>", currentTime);
				  printf("403 Forbidden\n");
				  send(socket, str, strlen(str), 0);
				  break;

		case 404: snprintf(str, sizeof(str), "HTTP/1.1 404 Not Found\r\nContent-Length: 91\r\nContent-Type: text/html\r\nConnection: keep-alive\r\nDate: %s\r\nServer: VaibhavN/14785\r\n\r\n<HTML><HEAD><TITLE>404 Not Found</TITLE></HEAD>\n<BODY><H1>404 Not Found</H1>\n</BODY></HTML>", currentTime);
				  printf("404 Not Found\n");
				  send(socket, str, strlen(str), 0);
				  break;

		case 500: snprintf(str, sizeof(str), "HTTP/1.1 500 Internal Server Error\r\nContent-Length: 115\r\nConnection: keep-alive\r\nContent-Type: text/html\r\nDate: %s\r\nServer: VaibhavN/14785\r\n\r\n<HTML><HEAD><TITLE>500 Internal Server Error</TITLE></HEAD>\n<BODY><H1>500 Internal Server Error</H1>\n</BODY></HTML>", currentTime);
				  //printf("500 Internal Server Error\n");
				  send(socket, str, strlen(str), 0);
				  break;

		case 501: snprintf(str, sizeof(str), "HTTP/1.1 501 Not Implemented\r\nContent-Length: 103\r\nConnection: keep-alive\r\nContent-Type: text/html\r\nDate: %s\r\nServer: VaibhavN/14785\r\n\r\n<HTML><HEAD><TITLE>404 Not Implemented</TITLE></HEAD>\n<BODY><H1>501 Not Implemented</H1>\n</BODY></HTML>", currentTime);
				  printf("501 Not Implemented\n");
				  send(socket, str, strlen(str), 0);
				  break;

		case 505: snprintf(str, sizeof(str), "HTTP/1.1 505 HTTP Version Not Supported\r\nContent-Length: 125\r\nConnection: keep-alive\r\nContent-Type: text/html\r\nDate: %s\r\nServer: VaibhavN/14785\r\n\r\n<HTML><HEAD><TITLE>505 HTTP Version Not Supported</TITLE></HEAD>\n<BODY><H1>505 HTTP Version Not Supported</H1>\n</BODY></HTML>", currentTime);
				  printf("505 HTTP Version Not Supported\n");
				  send(socket, str, strlen(str), 0);
				  break;

		default:  return -1;

	}
	return 1;
}

int connectRemoteServer(char* host_addr, int port_num)
{
	// Try to get from pool first
	int remoteSocket = get_from_pool(host_addr, port_num);
	if (remoteSocket != -1) {
		return remoteSocket;
	}

	// Creating Socket for remote server ---------------------------

	remoteSocket = socket(AF_INET, SOCK_STREAM, 0);

	if( remoteSocket < 0)
	{
		printf("Error in Creating Socket.\n");
		return -1;
	}
	
	// Get host by the name or ip address provided
	// Using getaddrinfo instead of gethostbyname for thread safety (#11)
	struct addrinfo hints, *res;
	memset(&hints, 0, sizeof(hints));
	hints.ai_family = AF_INET;
	hints.ai_socktype = SOCK_STREAM;

	char port_str[16];
	snprintf(port_str, sizeof(port_str), "%d", port_num);

	int status = getaddrinfo(host_addr, port_str, &hints, &res);
	if(status != 0)
	{
		fprintf(stderr, "No such host exists: %s\n", gai_strerror(status));
		close(remoteSocket);  // Fix #8: close socket on error
		return -1;
	}

	// Connect to Remote server ----------------------------------------------------

	if( connect(remoteSocket, res->ai_addr, res->ai_addrlen) < 0 )
	{
		fprintf(stderr, "Error in connecting !\n");
		close(remoteSocket);  // Fix #8: close socket on error
		freeaddrinfo(res);
		return -1;
	}

    // Set 1-second receive timeout to prevent hangs on keep-alive connections
    struct timeval tv;
    tv.tv_sec = 1;
    tv.tv_usec = 0;
    setsockopt(remoteSocket, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof(tv));

	freeaddrinfo(res);
	return remoteSocket;
}


int handle_request(int clientSocket, struct ParsedRequest *request, char *tempReq)
{
	char *buf = (char*)malloc(MAX_BYTES);
	if(buf == NULL){
		perror("malloc failed for buf");
		return -1;
	}
	strcpy(buf, "GET ");
	strcat(buf, request->path);
	strcat(buf, " ");
	strcat(buf, request->version);
	strcat(buf, "\r\n");

	size_t len = strlen(buf);

	if (ParsedHeader_set(request, "Connection", "keep-alive") < 0){
		printf("set header key not work\n");
	}

	if(ParsedHeader_get(request, "Host") == NULL)
	{
		if(ParsedHeader_set(request, "Host", request->host) < 0){
			printf("Set \"Host\" header key not working\n");
		}
	}

	if (ParsedRequest_unparse_headers(request, buf + len, (size_t)MAX_BYTES - len) < 0) {
		printf("unparse failed\n");
		//return -1;				// If this happens Still try to send request without header
	}

	int server_port = 80;				// Default Remote Server Port
	if(request->port != NULL)
		server_port = atoi(request->port);

	int remoteSocketID = connectRemoteServer(request->host, server_port);

	if(remoteSocketID < 0){
		free(buf);
		return -1;
	}

	int bytes_sent = send(remoteSocketID, buf, strlen(buf), 0);
	if(bytes_sent < 0){
		perror("Error sending request to remote server");
		free(buf);
		close(remoteSocketID);
		return -1;
	}

	memset(buf, 0, MAX_BYTES);

	// Fix #3: Use separate variables for recv and send to prevent data corruption
	int bytes_recv = recv(remoteSocketID, buf, MAX_BYTES-1, 0);
	char *temp_buffer = (char*)malloc(MAX_BYTES);
	if(temp_buffer == NULL){
		perror("malloc failed for temp_buffer");
		free(buf);
		close(remoteSocketID);
		return -1;
	}
	int temp_buffer_size = MAX_BYTES;
	int temp_buffer_index = 0;

	while(bytes_recv > 0)
	{
		// Send all received bytes to the client (handle partial sends)
		int total_sent = 0;
		while(total_sent < bytes_recv){
			bytes_sent = send(clientSocket, buf + total_sent, bytes_recv - total_sent, 0);
			if(bytes_sent < 0){
				perror("Error in sending data to client socket.\n");
				goto cleanup;
			}
			total_sent += bytes_sent;
		}
		
		// Copy received data into temp_buffer for caching
		// Fix #12: Only grow when needed, and handle realloc failure
		if(temp_buffer_index + bytes_recv >= temp_buffer_size){
			while(temp_buffer_index + bytes_recv >= temp_buffer_size)
				temp_buffer_size *= 2;
			char *new_buf = (char*)realloc(temp_buffer, temp_buffer_size);
			if(new_buf == NULL){
				perror("realloc failed for temp_buffer");
				goto cleanup;
			}
			temp_buffer = new_buf;
		}
		memcpy(temp_buffer + temp_buffer_index, buf, bytes_recv);
		temp_buffer_index += bytes_recv;

		memset(buf, 0, MAX_BYTES);
		bytes_recv = recv(remoteSocketID, buf, MAX_BYTES-1, 0);
	} 

cleanup:
	temp_buffer[temp_buffer_index]='\0';
	free(buf);
	add_cache_element(temp_buffer, temp_buffer_index, tempReq);
	printf("Done\n");
	free(temp_buffer);

 	return_to_pool(request->host, server_port, remoteSocketID);
	return 0;
}

int checkHTTPversion(char *msg)
{
	int version = -1;

	if(strncmp(msg, "HTTP/1.1", 8) == 0)
	{
		version = 1;
	}
	else if(strncmp(msg, "HTTP/1.0", 8) == 0)			
	{
		version = 1;										// Handling this similar to version 1.1
	}
	else
		version = -1;

	return version;
}


void* thread_fn(void* socketNew)
{
	sem_wait(&seamaphore); 
	int p;
	sem_getvalue(&seamaphore,&p);
	printf("semaphore value:%d\n",p);
    int* t= (int*)(socketNew);
	int socket=*t;           // Socket is socket descriptor of the connected Client
	int bytes_send_client,len;	  // Bytes Transferred

	
	char *buffer = (char*)calloc(MAX_BYTES, 1);	// Creating buffer of 4kb for a client
	if(buffer == NULL){
		perror("calloc failed for buffer");
		sem_post(&seamaphore);
		return NULL;
	}

	bytes_send_client = recv(socket, buffer, MAX_BYTES, 0); // Receiving the Request of client by proxy server
	
	while(bytes_send_client > 0)
	{
		len = strlen(buffer);
        //loop until u find "\r\n\r\n" in the buffer
		if(strstr(buffer, "\r\n\r\n") == NULL)
		{	
			bytes_send_client = recv(socket, buffer + len, MAX_BYTES - len, 0);
		}
		else{
			break;
		}
	}

	// printf("--------------------------------------------\n");
	// printf("%s\n",buffer);
	// printf("----------------------%d----------------------\n",strlen(buffer));
	
	// Fix #5: Use strcpy to ensure null terminator
	char *tempReq = (char*)malloc(strlen(buffer) + 1);
	if(tempReq == NULL){
		perror("malloc failed for tempReq");
		shutdown(socket, SHUT_RDWR);
		close(socket);
		free(buffer);
		sem_post(&seamaphore);
		return NULL;
	}
	strcpy(tempReq, buffer);
	
	// Fix #4: find_and_copy returns a safe copy of cached data (copied under lock)
	// instead of a raw pointer that could be freed by another thread.
cache_lookup: ;
	char *cached_data = NULL;
	int cached_len = 0;
	{
		int temp_lock_val = pthread_rwlock_rdlock(&rwlock);
		printf("Thread Find Cache Lock Acquired %d\n", temp_lock_val);

		unsigned int index = fnv1a_hash(tempReq);
		cache_element* site = hash_table[index];
		while (site != NULL) {
			if (!strcmp(site->url, tempReq)) {
				printf("\nurl found\n");
				// Update LRU position: move to tail (most recently used)
				pthread_mutex_lock(&lru_mutex);
				lru_remove(site);
				lru_append_tail(site);
				pthread_mutex_unlock(&lru_mutex);
				// Copy data while holding lock to prevent use-after-free
				cached_len = site->len;
				cached_data = (char*)malloc(cached_len + 1);
				if(cached_data != NULL){
					memcpy(cached_data, site->data, cached_len);
					cached_data[cached_len] = '\0';
				}
				break;
			}
			site = site->hash_next;
		}
		if (site == NULL) {
			printf("\nurl not found\n");
		}

		temp_lock_val = pthread_rwlock_unlock(&rwlock);
		printf("Thread Find Cache Lock Unlocked %d\n", temp_lock_val);
	}

	if( cached_data != NULL){
		// Fix #2: Send cached response with proper bounds checking
		int pos = 0;
		while(pos < cached_len){
			int to_send = (cached_len - pos < MAX_BYTES) ? cached_len - pos : MAX_BYTES;
			send(socket, cached_data + pos, to_send, 0);
			pos += to_send;
		}
		printf("Data retrieved from the Cache\n\n");
		free(cached_data);
	}
	
	
	else if(bytes_send_client > 0)
	{
		len = strlen(buffer); 
		//Parsing the request
		struct ParsedRequest* request = ParsedRequest_create();
		
        //ParsedRequest_parse returns 0 on success and -1 on failure.On success it stores parsed request in
        // the request
		if (ParsedRequest_parse(request, buffer, len) < 0) 
		{
		   	printf("Parsing failed\n");
		}
		else
		{	
			memset(buffer, 0, MAX_BYTES);
			if(!strcmp(request->method,"GET"))							
			{
                
				if( request->host && request->path && (checkHTTPversion(request->version) == 1) )
				{
					pending_entry_t* pending = pending_wait_or_register(tempReq);
					if (pending != NULL) {
						// Another thread is fetching this. Wait for it.
						pending_wait(pending);
						ParsedRequest_destroy(request);
						goto cache_lookup;
					} else {
						// We are the designated fetcher
						bytes_send_client = handle_request(socket, request, tempReq);		// Handle GET request
						pending_complete(tempReq);
						
						if(bytes_send_client == -1)
						{	
							sendErrorMessage(socket, 500);
						}
					}
				}
				else
					sendErrorMessage(socket, 500);			// 500 Internal Error

			}
            else
            {
                printf("This code doesn't support any method other than GET\n");
            }
    
		}
        //freeing up the request pointer
		ParsedRequest_destroy(request);

	}

	else if( bytes_send_client < 0)
	{
		perror("Error in receiving from client.\n");
	}
	else if(bytes_send_client == 0)
	{
		printf("Client disconnected!\n");
	}

	shutdown(socket, SHUT_RDWR);
	close(socket);
	free(buffer);
	sem_post(&seamaphore);	
	
	sem_getvalue(&seamaphore,&p);
	printf("Semaphore post value:%d\n",p);
	free(tempReq);
	return NULL;
}


int main(int argc, char * argv[]) {

	int client_socketId, client_len; // client_socketId == to store the client socket id
	struct sockaddr_in server_addr, client_addr; // Address of client and server to be assigned

	lru_init(); // Initialize the LRU doubly linked list
	init_connection_pool(); // Initialize the connection pool
    for (int i = 0; i < MAX_PENDING; i++) pending_table[i] = NULL;
    pthread_mutex_init(&pending_lock, NULL);
    sem_init(&seamaphore,0,MAX_CLIENTS); // Initializing seamaphore and lock
    pthread_rwlock_init(&rwlock,NULL); // Initializing rwlock for cache
    pthread_mutex_init(&lru_mutex, NULL);

    // Initialize hash table buckets to NULL
    for (int i = 0; i < HASH_TABLE_SIZE; i++) {
        hash_table[i] = NULL;
    }

	if(argc == 2)        //checking whether two arguments are received or not
	{
		port_number = atoi(argv[1]);
	}
	else
	{
		printf("Too few arguments\n");
		exit(1);
	}

	printf("Setting Proxy Server Port : %d\n",port_number);

    //creating the proxy socket
	proxy_socketId = socket(AF_INET, SOCK_STREAM, 0);

	if( proxy_socketId < 0)
	{
		perror("Failed to create socket.\n");
		exit(1);
	}

	int reuse =1;
	if (setsockopt(proxy_socketId, SOL_SOCKET, SO_REUSEADDR, (const char*)&reuse, sizeof(reuse)) < 0) 
        perror("setsockopt(SO_REUSEADDR) failed\n");

	memset((char*)&server_addr, 0, sizeof(server_addr));  
	server_addr.sin_family = AF_INET;
	server_addr.sin_port = htons(port_number); // Assigning port to the Proxy
	server_addr.sin_addr.s_addr = INADDR_ANY; // Any available adress assigned

    // Binding the socket
	if( bind(proxy_socketId, (struct sockaddr*)&server_addr, sizeof(server_addr)) < 0 )
	{
		perror("Port is not free\n");
		exit(1);
	}
	printf("Binding on port: %d\n",port_number);

    // Proxy socket listening to the requests
	int listen_status = listen(proxy_socketId, MAX_CLIENTS);

	if(listen_status < 0 )
	{
		perror("Error while Listening !\n");
		exit(1);
	}

	int i = 0; // Iterator for thread_id (tid) and Accepted Client_Socket for each thread
	int Connected_socketId[MAX_CLIENTS];   // This array stores socket descriptors of connected clients

    // Infinite Loop for accepting connections
	while(1)
	{
		
		memset((char*)&client_addr, 0, sizeof(client_addr));			// Clears struct client_addr
		client_len = sizeof(client_addr); 

        // Accepting the connections
		client_socketId = accept(proxy_socketId, (struct sockaddr*)&client_addr,(socklen_t*)&client_len);	// Accepts connection
		if(client_socketId < 0)
		{
			fprintf(stderr, "Error in Accepting connection !\n");
			exit(1);
		}
		else{
			Connected_socketId[i] = client_socketId; // Storing accepted client into array
		}

		// Getting IP address and port number of client
		struct sockaddr_in* client_pt = (struct sockaddr_in*)&client_addr;
		struct in_addr ip_addr = client_pt->sin_addr;
		char str[INET_ADDRSTRLEN];										// INET_ADDRSTRLEN: Default ip address size
		inet_ntop( AF_INET, &ip_addr, str, INET_ADDRSTRLEN );
		printf("Client is connected with port number: %d and ip address: %s \n",ntohs(client_addr.sin_port), str);
		//printf("Socket values of index %d in main function is %d\n",i, client_socketId);
		pthread_create(&tid[i],NULL,thread_fn, (void*)&Connected_socketId[i]); // Creating a thread for each client accepted
		// Fix #7: Wrap index to prevent out-of-bounds access after MAX_CLIENTS connections
		i = (i + 1) % MAX_CLIENTS;
	}
	close(proxy_socketId);									// Close socket
 	return 0;
}

void lru_init() {
    lru_head = NULL;
    lru_tail = NULL;
}

void lru_remove(cache_element* element) {
    if (element->lru_prev) element->lru_prev->lru_next = element->lru_next;
    else lru_head = element->lru_next;
    if (element->lru_next) element->lru_next->lru_prev = element->lru_prev;
    else lru_tail = element->lru_prev;
    element->lru_prev = NULL;
    element->lru_next = NULL;
}

void lru_append_tail(cache_element* element) {
    element->lru_prev = lru_tail;
    element->lru_next = NULL;
    if (lru_tail) lru_tail->lru_next = element;
    lru_tail = element;
    if (lru_head == NULL) lru_head = element;
}

cache_element* lru_get_lru() { return lru_head; }

// Note: find() is now only used externally for diagnostics.
// The main cache lookup in thread_fn was inlined with lock-safe copying (Fix #4).
cache_element* find(char* url){
// Checks for url in the cache using hashtable lookup.
// Returns pointer to the cache element if found, NULL otherwise.
// WARNING: The returned pointer is only safe while the caller holds the lock.
    cache_element* site = NULL;
    int temp_lock_val = pthread_rwlock_rdlock(&rwlock);
	printf("Find Cache Lock Acquired %d\n", temp_lock_val);

    unsigned int index = fnv1a_hash(url);
    site = hash_table[index];
    while (site != NULL) {
        if (!strcmp(site->url, url)) {
            printf("\nurl found\n");
            // Update LRU position safely with a separate lock
            pthread_mutex_lock(&lru_mutex);
            lru_remove(site);
            lru_append_tail(site);
            pthread_mutex_unlock(&lru_mutex);
            break;
        }
        site = site->hash_next;
    }
    if (site == NULL) {
        printf("\nurl not found\n");
    }

    temp_lock_val = pthread_rwlock_unlock(&rwlock);
	printf("Find Cache Lock Unlocked %d\n", temp_lock_val);
    return site;
}

// Fix #1: Split into unlocked helper + locked wrapper to prevent deadlock
// when add_cache_element (which holds lock) needs to evict entries.
static void _remove_cache_element_unlocked(){
    // PRECONDITION: caller must hold &lock
    // If cache is not empty, get the least recently used element (lru_head)
    // and remove it from both the LRU linked list and the hash table.
    cache_element* temp = lru_get_lru(); // element to remove (LRU victim)

	if (temp != NULL) {
        // Remove from LRU linked list
        lru_remove(temp);

        // Remove from hash table bucket chain
        unsigned int index = fnv1a_hash(temp->url);
        cache_element* h = hash_table[index];
        if (h == temp) {
            // Element is the head of the bucket chain
            hash_table[index] = temp->hash_next;
        } else {
            // Walk the bucket chain to find the predecessor
            while (h != NULL && h->hash_next != temp) {
                h = h->hash_next;
            }
            if (h != NULL) {
                h->hash_next = temp->hash_next;
            }
        }

		cache_size = cache_size - (temp->len) - sizeof(cache_element) -
		             strlen(temp->url) - 1;     // updating the cache size
		free(temp->data);
		free(temp->url);
		free(temp);
	}
}

// Public wrapper: acquires lock, evicts one LRU element, releases lock
void remove_cache_element(){
    int temp_lock_val = pthread_rwlock_wrlock(&rwlock);
	printf("Remove Cache Lock Acquired %d\n", temp_lock_val);

	_remove_cache_element_unlocked();

    temp_lock_val = pthread_rwlock_unlock(&rwlock);
	printf("Remove Cache Lock Unlocked %d\n", temp_lock_val);
}

int add_cache_element(char* data, int size, char* url){
    // Adds element to both the LRU linked list and the hash table.
    int temp_lock_val = pthread_rwlock_wrlock(&rwlock);
	printf("Add Cache Lock Acquired %d\n", temp_lock_val);

    int element_size = size + 1 + strlen(url) + sizeof(cache_element);
    if (element_size > MAX_ELEMENT_SIZE) {
        // Element too large to cache
        temp_lock_val = pthread_rwlock_unlock(&rwlock);
		printf("Add Cache Lock Unlocked %d\n", temp_lock_val);
        return 0;
    }
    else
    {
        while (cache_size + element_size > MAX_SIZE) {
            // Fix #1: Call unlocked version since we already hold the lock
            _remove_cache_element_unlocked();
        }

        cache_element* element = (cache_element*)malloc(sizeof(cache_element));
        if(element == NULL){
            perror("malloc failed for cache element");
            temp_lock_val = pthread_rwlock_unlock(&rwlock);
            return 0;
        }
        element->data = (char*)malloc(size + 1);
        if(element->data == NULL){
            perror("malloc failed for cache element data");
            free(element);
            temp_lock_val = pthread_rwlock_unlock(&rwlock);
            return 0;
        }
        memcpy(element->data, data, size);
        element->data[size] = '\0';
        element->url = (char*)malloc(strlen(url) + 1);
        if(element->url == NULL){
            perror("malloc failed for cache element url");
            free(element->data);
            free(element);
            temp_lock_val = pthread_rwlock_unlock(&rwlock);
            return 0;
        }
        strcpy(element->url, url);
        element->len = size;

        // Insert at tail of LRU linked list (most recently used)
        lru_append_tail(element);

        // Insert at head of hash table bucket chain
        // hash disabled

        cache_size += element_size;
        temp_lock_val = pthread_rwlock_unlock(&rwlock);
		printf("Add Cache Lock Unlocked %d\n", temp_lock_val);
        return 1;
    }
    return 0;
}