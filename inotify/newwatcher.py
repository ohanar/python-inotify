# watcher.py - high-level interfaces to the Linux inotify subsystem

# Copyright 2006 Bryan O'Sullivan <bos@serpentine.com>
# Copyright 2012-2013 Jan Kanis <jan.code@jankanis.nl>

# This library is free software; you can redistribute it and/or modify
# it under the terms of version 2.1 of the GNU Lesser General Public
# License, incorporated herein by reference.

# Additionally, code written by Jan Kanis may also be redistributed and/or 
# modified under the terms of any version of the GNU Lesser General Public 
# License greater than 2.1. 

'''High-level interfaces to the Linux inotify subsystem.

The inotify subsystem provides an efficient mechanism for file status
monitoring and change notification.

The Watcher class hides the low-level details of the inotify
interface, and provides a Pythonic wrapper around it.  It generates
events that provide somewhat more information than raw inotify makes
available.

The AutoWatcher class is more useful, as it automatically watches
newly-created directories on your behalf.'''

__author__ = "Jan Kanis <jan.code@jankanis.nl>"

from . import constants
from . import _inotify as inotify
from .watcher import InotifyWatcherException, NoFilesException
import functools
import operator
import array
import errno
import fcntl
import os
from os import path
import termios
from collections import namedtuple, defaultdict


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
    'link_changed': 'The named path no longer resolves to the same file',
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
    }
_watch_props.update(_inotify_props)


# TODO: move this to __init__.py

inotify_builtin_constants = functools.reduce(operator.or_, constants.values())
inotify.IN_LINK_CHANGED = 1
while inotify.IN_LINK_CHANGED < inotify_builtin_constants:
    inotify.IN_LINK_CHANGED <<= 1
constants['IN_LINK_CHANGED'] = inotify.IN_LINK_CHANGED

def decode_mask(mask):
    d = inotify.decode_mask(mask & inotify_builtin_constants)
    if mask & inotify.IN_LINK_CHANGED:
        d.append('IN_LINK_CHANGED')
    return d



def _make_getter(name, doc):
    def getter(self, mask=constants['IN_' + name.upper()]):
        return self.mask & mask
    getter.__name__ = name
    getter.__doc__ = doc
    return getter





class Event(object):
    '''
    Derived inotify event class.

    The following fields are available:

        mask: event mask, indicating what kind of event this is

        cookie: rename cookie, if a rename-related event

        fullpath: the full path of the file or directory to which the event
        occured. If this watch has more than one path, a path is chosen
        arbitrarily.

        paths: a list of paths that resolve to the watched file/directory

        name: name of the directory entry to which the event occurred
        (may be None if the event happened to a watched directory)

        wd: watch descriptor that triggered this event

    '''

    __slots__ = (
        'cookie',
        'mask',
        'name',
        'raw',
        'path',
        )

    @property
    def fullpath(self):
        if self.name:
            return path.join(self.path, self.name)
        return self.path

    def __init__(self, raw, path):
        self.raw = raw
        self.path = path
        self.mask = raw.mask
        self.cookie = raw.cookie
        self.name = raw.name
    
    def __repr__(self):
        r = 'Event(path={}, mask={}'.format(repr(self.path), '|'.join(decode_mask(self.mask)))
        if self.cookie:
            r += ', cookie={}'.format(self.cookie)
        if self.name:
            r += ', name={}'.format(repr(self.name))
        r += ')'
        return r


for name, doc in _event_props.items():
    setattr(Event, name, property(_make_getter(name, doc), doc=doc))





class Watcher (object):

    def __init__(self):
        self.fd = inotify.init()
        self._watchdescriptors = {}
        self._paths = {}
        self._buffer = []
        self._reread_required = None

    def add(self, pth, mask):
        if pth in self._paths:
            self._paths[pth].update_mask(mask)
            return
        self._paths[pth] = _Watch(self, pth, mask)

    def _createwatch(self, pth, name, mask, callback):
        wd = inotify.add_watch(self.fd, pth, mask | inotify.IN_MASK_ADD)
        if not wd in self._watchdescriptors:
            self._watchdescriptors[wd] = _Descriptor(self, wd)
        desc = self._watchdescriptors[wd]
        desc.add_callback(pth, mask, name, callback)
        return desc

    def _removewatch(self, descriptor):
        del self._watchdescriptors[descriptor.wd]

    def _signal_empty_descriptor(self, descriptor):
        '''This method is called from a _Descriptor instance if it no longer has any
        callbacks attached to it and so should be deleted. This means
        inotify.read may need to be called again to catch the corresponding
        IN_IGNORE event.
        '''
        inotify.remove_watch(self.fd, descriptor.wd)
        if not self._reread_required is None:
            self._reread_required = True

    def read(self, block=True, bufsize=None):
        '''Read a list of queued inotify events.

        If bufsize is zero, only return those events that can be read
        immediately without blocking.  Otherwise, block until events are
        available.'''

        if not block:
            bufsize = 0
        elif bufsize == 0:
            bufsize = None

        if not len(self._watchdescriptors):
            raise NoFilesException("There are no files to watch")

        # If a watch descriptor is removed during event processing, we want to
        # call inotify.read again to catch and process the IN_IGNORE
        # events. What we need is a dynamically scoped variable that can be set
        # somewhere down the call stack during the event processing. Since
        # Python doesn't have dynamic variables we use an instance variable and
        # check that it is in the correnct state. This is from a design point a
        # bit unfortunate as this variable really only has a meaning while the
        # call to Watcher.read is active on the stack.
        assert self._reread_required is None
        events = []
        while self._reread_required in (None, True):
            self._reread_required = False
            for evt in inotify.read(self.fd, bufsize):
                for e in self._watchdescriptors[evt.wd].handle_event(evt):
                    events.append(e)
        self._reread_required = None
        return events

    def close(self):
        os.close(self.fd)


class _Watch (object):
    def __init__(self, watcher, pth, mask):
        self.watcher = watcher
        self.path = self._normpath(pth)
        self.cwd = os.getcwd()
        self.mask = mask
        self.links = []
        # self.inode = None
        self.add(pth)

    def _normpath(self, pth):
        split = [p for p in pth.split(path.sep) if p not in ('', '.')]
        if pth.startswith('/'):
            split.insert(0, '/')
        return split

    def _nonrel(self, pth):
        '''Return the path joined with the working directory at the time this watch was
        created.
        '''
        return path.join(self.cwd, pth)

    def add(self, pth):
        # Register symlinks in a non-racy way
        linkdepth = 0
        while True:
            try:
                link = os.readlink(pth)
            except OSError as e:
                if e.errno == os.errno.EINVAL:
                    # The entry is not a symbolic link
                    break
                if e.errno in (os.errno.ENOENT, os.errno.ENOTDIR):
                    # The entry does not exist, or the path is not valid
                    if linkdepth == 0:
                        raise InotifyWatcherException("File does not exist: "+pth)
                    # the originally passed path exists, but it is a broken symlink
                    return
                raise
            self.add_symlink(pth)
            pth = path.join(path.dirname(pth), link)
            linkdepth += 1

        self.add_leaf(pth)

    def add_leaf(self, pth):
        mask = self.mask | inotify.IN_MOVE_SELF | inotify.IN_DELETE_SELF
        self.links.append(_Link(len(self.links), 'leaf', self, mask, pth, None))
        # st = os.stat(pth)
        # self.inode = (st.st_dev, st.st_ino)

    def add_symlink(self, pth):
        pth, name = path.split(pth)
        if not pth:
            pth = '.'
        mask = inotify.IN_MOVE | inotify.IN_DELETE | inotify.IN_CREATE | inotify.IN_ONLYDIR
        self.links.append(_Link(len(self.links), 'symlink', self, mask, pth, name))
        
    def handle_event(self, event, pth):
        if pth.idx == len(self.links) - 1:
            assert event.mask & self.mask
            yield Event(event, path.join(*self.path))
        else:
            for p in self.links[pth.idx:]:
                p.remove()
            del self.links[pth.idx:]
            yield Event(mediumevent(mask=inotify.IN_LINK_CHANGED, cookie=0, name=None, wd=event.wd), path.join(*self.path)), False
             

mediumevent = namedtuple('mediumevent', 'mask cookie name wd')


class _Link (object):
    def __init__(self, idx, typ, watch, mask, pth, name):
        self.idx = idx
        self.type = typ
        self.watch = watch
        self.mask = mask
        self.path = pth
        self.name = name
        self.wd = watch.watcher._createwatch(self.path, self.name, mask, self.handle_event)

    def handle_event(self, event):
        yield from self.watch.handle_event(event, self)

    def remove(self):
        self.wd.remove_callback(self.name, self.handle_event)
    

class _Descriptor (object):

    def __init__(self, watcher, wd):
        self.watcher = watcher
        self.wd = wd
        self.mask = 0
        # callbacks is indexed by name to improve speed and because we
        # can. Indexing by name and mask would be faster but would be more
        # cumbersome to implement.
        self.callbacks = defaultdict(list)

    def add_callback(self, pth, mask, name, callback):
        # If the callback is to a path link element, mask will include
        # IN_ONLYDIR so we could remove that here. However the IN_ONLYDIR flag
        # can not be returned by inotify events so keeping it in does no harm.
        self.mask |= mask
        self.callbacks[name].append((mask, callback))

    def remove_callback(self, name, callback):
        idx = [c == callback for m,c in self.callbacks[name]].index(True)
        del self.callbacks[name][idx]
        if not self.callbacks[name]:
            del self.callbacks[name]
        if not self.callbacks:
            self.watcher._signal_empty_descriptor(self)

    def handle_event(self, event):
        for m, c in self.callbacks[event.name]:
            if event.mask & m:
                yield from c(event)
        if event.mask & inotify.IN_IGNORED:
            self.watcher._removewatch(self)
      
