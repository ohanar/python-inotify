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
import os, sys
from os import path
import termios
from collections import namedtuple, defaultdict
from pathlib import PosixPath


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
        pth = PosixPath(pth)
        if pth in self._paths:
            self._paths[pth].update_mask(mask)
            return
        self._paths[pth] = _Watch(self, pth, mask)

    def _createwatch(self, pth, name, mask, callback):
        wd = inotify.add_watch(self.fd, str(pth), mask | inotify.IN_MASK_ADD)
        if not wd in self._watchdescriptors:
            self._watchdescriptors[wd] = _Descriptor(self, wd)
        desc = self._watchdescriptors[wd]
        desc.add_callback(mask, name, callback)
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
    root = PosixPath('/')
    cwd = PosixPath('.')
    parentdir = PosixPath('..')
    
    def __init__(self, watcher, pth, mask):
        self.watcher = watcher
        self.path = PosixPath(pth)
        self.cwd = PosixPath.cwd()
        self.mask = mask
        self.links = []
        self.complete_watch = False
        # self.inode = None
        self.reconnect()




            
        

    @staticmethod
    def paths(path):
        # empty path and the current dir is represented the same in pathlib
        none = _Watch.cwd

        if path.is_absolute():
            dir = _Watch.root
            path = path.relative()
        else:
            dir = _Watch.cwd
        name = path.parts[0:1]
        rest = path.parts[1:]

        yield (dir, name, rest, 'path')

        while name != none:
            dir, name, rest, *type = _Watch.nextpath(dir, name, rest)
            if name == _Watch.parentdir:
                dir = dir.parent()
                name = rest.parts[0:1]
                rest = rest.parts[1:]
            if name == none:
                type = 'target'
            yield (dir, name, rest) + tuple(type)
        

    @staticmethod
    def nextpath(dir, name, rest):
        # Test if it is a symlink
        try:
            link = os.readlink(str(dir[name]))
        except OSError as e:
            if e.errno == os.errno.EINVAL:
                # The entry is not a symbolic link, assume it is a normal file
                # or directory
                return (dir[name], rest.parts[0:1], rest.parts[1:], 'path')
            if e.errno == os.errno.ENOENT:
                # The entry does not exist, or the path is not valid
                return (dir, name, rest, 'error', 'ENOENT')
            if e.errno == os.errno.ENOTDIR:
                # A directory along the path changed, path is no longer
                # valid. We should have received an event about this so abort
                # now and re-establish when we receive the event.
                return (dir, name, rest, 'error', 'ENOTDIR')
            raise
        else:
            # it is a link
            rest = PosixPath(link)[rest]
            if rest.is_absolute():
                dir = _Watch.root
                rest = rest.relative()
            # else dir remains the current dir
            return (dir, rest.parts[0:1], rest.parts[1:], 'symlink')
        
        assert False


    def reconnect(self):
        # seen_links = set()
        
        # Register symlinks and path elements in a non-racy way
        pth = self.path
        linkdepth = 0
        while True:
            try:
                link = os.readlink(str(pth))
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
            pth = pth.parent()[link]
            linkdepth += 1

        self.add_leaf(pth)

    def add_leaf(self, pth):
        mask = self.mask | inotify.IN_MOVE_SELF | inotify.IN_DELETE_SELF
        self.links.append(_Link(len(self.links), 'leaf', self, mask, pth, None))
        self.complete_watch = True
        # st = os.stat(pth)
        # self.inode = (st.st_dev, st.st_ino)

    def add_symlink(self, pth):
        name = pth.parts[-1:]
        pth = pth.parent()
        mask = inotify.IN_MOVE | inotify.IN_DELETE | inotify.IN_CREATE | inotify.IN_ONLYDIR
        self.links.append(_Link(len(self.links), 'symlink', self, mask, pth, name))
        
    def handle_event(self, event, link):
        if self.complete_watch and link.idx == len(self.links) - 1:
            assert event.mask & self.mask
            yield Event(event, str(self.path))
        else:
            for p in self.links[link.idx:]:
                p.remove()
            del self.links[link.idx:]
            self.complete_watch = False
            yield Event(mediumevent(mask=inotify.IN_LINK_CHANGED, cookie=0, name=None, wd=event.wd), str(self.path))

    def __str__(self):
        return '<_Watch for {}>'.format(str(self.path))
             

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

    def _fullname(self):
        if self.name:
            return str(self.path[self.name])
        return str(self.path)

    def __str__(self):
        return '<_Link for {}>'.format(self._fullname())
    

class _Descriptor (object):

    def __init__(self, watcher, wd):
        self.watcher = watcher
        self.wd = wd
        self.mask = 0
        # callbacks is indexed by name to improve speed and because we
        # can. Indexing by name and mask would be faster but would be more
        # cumbersome to implement.
        self.callbacks = {}

    def add_callback(self, mask, name, callback):
        # If the callback is to a path link element, mask will include
        # IN_ONLYDIR so we could remove that here. However the IN_ONLYDIR flag
        # can not be returned by inotify events so keeping it in does no harm.
        self.mask |= mask
        self.callbacks.setdefault(name, []).append((mask, callback))

    def remove_callback(self, name, callback):
        idx = [c == callback for m,c in self.callbacks[name]].index(True)
        del self.callbacks[name][idx]
        if not self.callbacks[name]:
            del self.callbacks[name]
        if not self.callbacks:
            self.watcher._signal_empty_descriptor(self)

    def handle_event(self, event):
        name = PosixPath(event.name) if not event.name is None else None
        for m, c in self.callbacks.get(name, ()):
            if event.mask & m:
                yield from c(event)
        if event.mask & inotify.IN_IGNORED:
            assert not self.callbacks
            self.watcher._removewatch(self)
      
    def __str__(self):
        names = ', '.join(c.__self__._fullname() for c in l for l in self.callbacks.values())
        return '<_Descriptor for wd {}: {}>'.format(self.wd, ', '.join(names))


class InvalidPathException (Exception):
    pass

class NoEntryException (InvalidPathException):
    def __init__(self, pth, *args):
        msg = "Path not valid: '{}' does not exist".format(pth)
        InvalidPathException.__init__(self, msg, *args)

class NotDirectoryException (InvalidPathException):
    def __init__(self, pth, *args):
        msg = "Path not valid: '{}' is not a directory".format(pth)
        InvalidPathException.__init__(self, msg, *args)

class ConcurrentFilesystemModificationException (InvalidPathException):
    def __init__(self, pth, *args):
        msg = "Path not valid: A concurrent change was detected while traversing '{}'".format(pth)
        InvalidPathException.__init__(self, msg, *args)

class SymlinkLoopException (InvalidPathException):
    def __init__(self, pth, *args):
        msg = ("Path not valid: The symlink at '{}' forms a symlink loop".format(pth)
        InvalidPathException.__init__(self, msg, *args)
