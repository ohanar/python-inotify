# __init__.py - low-level interfaces to the Linux inotify subsystem

# Copyright 2006 Bryan O'Sullivan <bos@serpentine.com>
# Copyright 2012-2013 Jan Kanis <jan.code@jankanis.nl>

# This library is free software; you can redistribute it and/or modify
# it under the terms of version 2.1 of the GNU Lesser General Public
# License, incorporated herein by reference.

# Additionally, code written by Jan Kanis may also be redistributed and/or 
# modified under the terms of any version of the GNU Lesser General Public 
# License greater than 2.1. 

'''
Low-level interface to the Linux inotify subsystem.

The inotify subsystem provides an efficient mechanism for file status
monitoring and change notification.

This package provides the low-level inotify system call interface and
associated constants and helper functions.

For a higher-level interface that remains highly efficient, use the
inotify.watcher package.
'''

__author__ = "Jan Kanis <jan.code@jankanis.nl>"

from ._inotify import *

constants = {k: v for k,v in globals().items() if k.startswith('IN_')}

procfs_path = '/proc/sys/fs/inotify'

def _read_procfs_value(name):
    def read_value():
        try:
            return int(open(procfs_path + '/' + name).read())
        except OSError as err:
            return None

    read_value.__doc__ = '''Return the value of the %s setting from /proc.

    If inotify is not enabled on this system, return None.''' % name

    return read_value


# Inotify flags that can be specified on a watch and can be returned in an event
_inotify_props = {
    'access': 'File was accessed',
    'modify': 'File was modified',
    'attrib': 'Attribute of a directory entry was changed',
    'close': 'File was closed',
    'close_write': 'File was closed after being written to',
    'close_nowrite': 'File was closed without being written to',
    'open': 'File was opened',
    'move': 'Directory entry was renamed',
    'moved_from': 'Directory entry was renamed from this name',
    'moved_to': 'Directory entry was renamed to this name',
    'create': 'Directory entry was created',
    'delete': 'Directory entry was deleted',
    'delete_self': 'The watched directory entry was deleted',
    'move_self': 'The watched directory entry was renamed',
    }

# Inotify flags that can only be returned in an event
_event_props = {
    'unmount': 'Directory was unmounted, and can no longer be watched',
    'q_overflow': 'Kernel dropped events due to queue overflow',
    'ignored': 'Directory entry is no longer being watched',
    'isdir': 'Event occurred on a directory',
    }
_event_props.update(_inotify_props)

# Inotify flags that can only be specified in a watch
_watch_props = {
    'dont_follow': "Don't dereference pathname if it is a symbolic link",
    'excl_unlink': "Don't generate events after the file has been unlinked",
    'onlydir': "Only watch pathname if it is a directory",
    'oneshot': "Monitor pathname for one event, then stop watching it",
    'mask_add': "Add this mask to the existing mask instead of replacing it",
    }
_watch_props.update(_inotify_props)



max_queued_events = _read_procfs_value('max_queued_events')
max_user_instances = _read_procfs_value('max_user_instances')
max_user_watches = _read_procfs_value('max_user_watches')
