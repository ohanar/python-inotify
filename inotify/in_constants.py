# constants.py - submodule containing inotify constants and descriptions

# Copyright 2006 Bryan O'Sullivan <bos@serpentine.com>
# Copyright 2012-2013 Jan Kanis <jan.code@jankanis.nl>

# This library is free software; you can redistribute it and/or modify
# it under the terms of version 2.1 of the GNU Lesser General Public
# License, incorporated herein by reference.

# Additionally, code written by Jan Kanis may also be redistributed and/or
# modified under the terms of any version of the GNU Lesser General Public
# License greater than 2.1.

from . import _inotify

constants = {k: v for k,v in _inotify.__dict__.items() if k.startswith('IN_')}

# Inotify flags that can be specified on a watch and can be returned in an event
_inotify_properties = {
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
event_properties = {
    'unmount': 'Directory was unmounted, and can no longer be watched',
    'q_overflow': 'Kernel dropped events due to queue overflow',
    'ignored': 'Directory entry is no longer being watched',
    'isdir': 'Event occurred on a directory',
    }
event_properties.update(_inotify_properties)

# Inotify flags that can only be specified in a watch
watch_properties = {
    'dont_follow': "Don't dereference pathname if it is a symbolic link",
    'excl_unlink': "Don't generate events after the file has been unlinked",
    'onlydir': "Only watch pathname if it is a directory",
    'oneshot': "Monitor pathname for one event, then stop watching it",
    'mask_add': "Add this mask to the existing mask instead of replacing it",
    }
watch_properties.update(_inotify_properties)


combined_masks = set('IN_ALL_EVENTS IN_MOVE IN_CLOSE'.split())
def decode_mask(mask):
    return [name for name, m in constants.items() if not name in combined_masks and m & mask]

