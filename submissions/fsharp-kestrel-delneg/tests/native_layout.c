// Prints the kernel/glibc layout and constants that Transport.fs hard-codes, as "NAME value" lines.
// Build and run on the target box: cc -O0 native_layout.c -o /tmp/native_layout && /tmp/native_layout
#define _GNU_SOURCE
#include <stdio.h>
#include <stddef.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

#define P(name, v) printf("%s %ld\n", name, (long)(v))
int main(void) {
    P("sizeof_epoll_event", sizeof(struct epoll_event));
    P("offsetof_epoll_event_events", offsetof(struct epoll_event, events));
    P("offsetof_epoll_event_data", offsetof(struct epoll_event, data));
    P("alignof_epoll_event", _Alignof(struct epoll_event));
    P("sizeof_sockaddr_in", sizeof(struct sockaddr_in));
    P("offsetof_sockaddr_in_family", offsetof(struct sockaddr_in, sin_family));
    P("sizeof_sockaddr_in_family", sizeof(((struct sockaddr_in *)0)->sin_family));
    P("offsetof_sockaddr_in_port", offsetof(struct sockaddr_in, sin_port));
    P("offsetof_sockaddr_in_addr", offsetof(struct sockaddr_in, sin_addr));
    P("sizeof_int", sizeof(int));
    P("sizeof_size_t", sizeof(size_t));
    P("sizeof_ssize_t", sizeof(ssize_t));
    P("sizeof_socklen_t", sizeof(socklen_t));
    P("sizeof_eventfd_t", sizeof(eventfd_t));
    // Constants, named as in Transport.fs.
    P("AF_INET", AF_INET);
    P("SOCK_STREAM", SOCK_STREAM);
    P("SOCK_NONBLOCK", SOCK_NONBLOCK);
    P("SOCK_CLOEXEC", SOCK_CLOEXEC);
    P("SOL_SOCKET", SOL_SOCKET);
    P("SO_REUSEADDR", SO_REUSEADDR);
    P("IPPROTO_TCP", IPPROTO_TCP);
    P("TCP_NODELAY", TCP_NODELAY);
    P("EPOLL_CTL_ADD", EPOLL_CTL_ADD);
    P("EPOLL_CTL_DEL", EPOLL_CTL_DEL);
    P("EPOLL_CTL_MOD", EPOLL_CTL_MOD);
    P("EPOLLIN", EPOLLIN);
    P("EPOLLOUT", EPOLLOUT);
    P("EPOLLERR", EPOLLERR);
    P("EPOLLHUP", EPOLLHUP);
    P("EAGAIN", EAGAIN);
    P("EINTR", EINTR);
    P("MSG_NOSIGNAL", MSG_NOSIGNAL);
    // Transport.fs passes SOCK_CLOEXEC to epoll_create1 and SOCK_NONBLOCK|SOCK_CLOEXEC to eventfd: these must match.
    P("EPOLL_CLOEXEC", EPOLL_CLOEXEC);
    P("EFD_NONBLOCK", EFD_NONBLOCK);
    P("EFD_CLOEXEC", EFD_CLOEXEC);
    P("SHUT_RDWR", SHUT_RDWR);
    return 0;
}
