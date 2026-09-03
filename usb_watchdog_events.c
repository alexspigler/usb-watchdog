#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>

#include <stdbool.h>
#include <stdio.h>
#include <string.h>

static bool emit_events = false;

static void emit_line(const char *line) {
    if (puts(line) == EOF || fflush(stdout) == EOF) {
        CFRunLoopStop(CFRunLoopGetCurrent());
    }
}

static void drain_devices(void *context, io_iterator_t iterator) {
    const char *event_name = context;
    io_object_t device;
    bool found = false;

    while ((device = IOIteratorNext(iterator)) != IO_OBJECT_NULL) {
        found = true;
        IOObjectRelease(device);
    }

    if (emit_events && found) {
        emit_line(event_name);
    }
}

static void emit_heartbeat(CFRunLoopTimerRef timer, void *context) {
    (void)timer;
    (void)context;
    emit_line("heartbeat");
}

static bool install_notification(
    IONotificationPortRef port,
    const char *service_class,
    const io_name_t notification_type,
    const char *event_name,
    io_iterator_t *iterator
) {
    CFMutableDictionaryRef matching = IOServiceMatching(service_class);
    if (matching == NULL) {
        return false;
    }

    kern_return_t result = IOServiceAddMatchingNotification(
        port,
        notification_type,
        matching,
        drain_devices,
        (void *)event_name,
        iterator
    );
    if (result != KERN_SUCCESS) {
        return false;
    }

    drain_devices((void *)event_name, *iterator);
    return true;
}

static void release_iterators(io_iterator_t *iterators, size_t count) {
    for (size_t index = 0; index < count; index++) {
        if (iterators[index] != IO_OBJECT_NULL) {
            IOObjectRelease(iterators[index]);
        }
    }
}

int main(int argc, char **argv) {
    if (argc != 2 || strcmp(argv[1], "--events") != 0) {
        fprintf(stderr, "usage: %s --events\n", argv[0]);
        return 2;
    }

    if (setvbuf(stdout, NULL, _IOLBF, 0) != 0) {
        perror("setvbuf");
        return 1;
    }

    IONotificationPortRef port = IONotificationPortCreate(kIOMainPortDefault);
    if (port == NULL) {
        fputs("could not create IOKit notification port\n", stderr);
        return 1;
    }

    CFRunLoopSourceRef source = IONotificationPortGetRunLoopSource(port);
    if (source == NULL) {
        fputs("could not create IOKit run-loop source\n", stderr);
        IONotificationPortDestroy(port);
        return 1;
    }
    CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopDefaultMode);

    io_iterator_t iterators[4] = {
        IO_OBJECT_NULL,
        IO_OBJECT_NULL,
        IO_OBJECT_NULL,
        IO_OBJECT_NULL,
    };
    bool installed = install_notification(
        port,
        "IOUSBHostDevice",
        kIOFirstPublishNotification,
        "usb-published",
        &iterators[0]
    ) && install_notification(
        port,
        "IOUSBHostDevice",
        kIOTerminatedNotification,
        "usb-terminated",
        &iterators[1]
    ) && install_notification(
        port,
        "IOUSBHostInterface",
        kIOFirstPublishNotification,
        "usb-published",
        &iterators[2]
    ) && install_notification(
        port,
        "IOUSBHostInterface",
        kIOTerminatedNotification,
        "usb-terminated",
        &iterators[3]
    );
    if (!installed) {
        fputs("could not register IOKit USB notifications\n", stderr);
        release_iterators(iterators, 4);
        IONotificationPortDestroy(port);
        return 1;
    }

    emit_events = true;
    if (puts("ready") == EOF || fflush(stdout) == EOF) {
        release_iterators(iterators, 4);
        IONotificationPortDestroy(port);
        return 1;
    }

    CFRunLoopTimerContext timer_context = {0, NULL, NULL, NULL, NULL};
    CFRunLoopTimerRef heartbeat = CFRunLoopTimerCreate(
        kCFAllocatorDefault,
        CFAbsoluteTimeGetCurrent() + 1.0,
        1.0,
        0,
        0,
        emit_heartbeat,
        &timer_context
    );
    if (heartbeat == NULL) {
        fputs("could not create event-monitor heartbeat\n", stderr);
        release_iterators(iterators, 4);
        IONotificationPortDestroy(port);
        return 1;
    }
    CFRunLoopAddTimer(CFRunLoopGetCurrent(), heartbeat, kCFRunLoopDefaultMode);

    CFRunLoopRun();

    CFRunLoopTimerInvalidate(heartbeat);
    CFRelease(heartbeat);
    release_iterators(iterators, 4);
    IONotificationPortDestroy(port);
    return 0;
}
