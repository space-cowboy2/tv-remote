/*
 * TVRemoteHub.app launcher.
 *
 * macOS only applies Local Network permission to processes with an app
 * identity. A bare python started by launchd has none, so its traffic to the
 * TV fails with EHOSTUNREACH (errno 65) even after "Allow". This launcher is
 * the app bundle's executable. It spawns the hub as a child (children inherit
 * the app as their "responsible process", so the permission applies), forwards
 * SIGTERM/SIGINT/SIGHUP, and exits with the child's status so launchd's
 * KeepAlive restarts it.
 *
 * Usage: TVRemoteHub <program> [args...]
 */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <program> [args...]\n", argv[0]);
        return 64;
    }
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    pid_t pid;
    int rc = posix_spawn(&pid, argv[1], NULL, NULL, &argv[1], environ);
    if (rc != 0) {
        fprintf(stderr, "TVRemoteHub: cannot start %s: %s\n", argv[1], strerror(rc));
        return 70;
    }
    child = pid;
    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 71;
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}
