# watcher.py - high-level interfaces to the Linux inotify subsystem

# Copyright 2012-2013 Jan Kanis <jan.code@jankanis.nl>

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

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

import os, os.path, sys
import errno
import functools
import operator
from collections import namedtuple

from pathlib import PosixPath

from . import constants, pathresolver
from . import _inotify
from .constants import decode_mask, event_properties, watch_properties
from .watcher import NoFilesException, _make_getter
from .pathresolver import SymlinkLoopError

globals().update(constants.constants)



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
            return os.path.join(self.path, self.name)
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




class PathWatcher (object):
    '''This watcher can watch file system paths for changes. Unlike the standard
    inotify system, this watcher also watches for any changes in the meaning of
    the given path (e.g. symlink changes or intermediate directory changes) and
    generates a IN_PATH_CHANGED event.'''

    def __init__(self):
        self.fd = _inotify.init()
        self._watchdescriptors = {}
        self._paths = {}
        self._buffer = []
        self._reread_required = None
        self._reconnect = []

    def fileno(self):
        '''Return the file descriptor this watcher uses.  Useful for passing to select
        and poll.
        '''
        return self.fd

    def add(self, path, mask):
        '''Add a watch with the given mask for path. If the path is already watched,
        update the mask according to Watcher.update_mask.

        '''
        pth = PosixPath(path)
        if pth in self._paths:
            self._paths[path].update_mask(mask)
            return
        self._paths[path] = _Watch(self, path, mask)

    def _createwatch(self, path, name, mask, callback):
        'create a new _Descriptor for path'
        wd = _inotify.add_watch(self.fd, str(path), mask | IN_MASK_ADD)
        if not wd in self._watchdescriptors:
            self._watchdescriptors[wd] = _Descriptor(self, wd)
        desc = self._watchdescriptors[wd]
        desc.add_callback(mask, name, callback)
        return desc

    def _removewatch(self, descriptor):
        '''actually remove a descriptor. This should be called after receiving an
        IN_IGNORE event for the descriptor.

        '''
        del self._watchdescriptors[descriptor.wd]

    def _signal_empty_descriptor(self, descriptor):
        '''This method is called from a _Descriptor instance if it no longer has any
        callbacks attached to it and so should be deleted. This means
        inotify.read may need to be called again to catch the corresponding
        IN_IGNORE event.
        '''
        _inotify.remove_watch(self.fd, descriptor.wd)
        if not self._reread_required is None:
            self._reread_required = True

    def _reconnect_required(self, watch):
        '''Register a watch to be .reconnect()'ed after event processing is finished'''
        self._reconnect.append(watch)

    def read(self, block=True, bufsize=None):
        '''Read a list of queued inotify events.

        block: If block is false, return only those events that can be read immediately.

        bufsize: The buffer size to use to read events. Only meaningful if
        block is True. If bufsize is too small, an error will occur.

        '''

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
        try:
            while self._reread_required in (None, True):
                self._reread_required = False
                for evt in _inotify.read(self.fd, bufsize):
                    for e in self._watchdescriptors[evt.wd].handle_event(evt):
                        events.append(e)
        finally:
            self._reread_required = None
        for w in self._reconnect:
            w.reconnect()
        del self._reconnect[:]
        return events

    def update_mask(self, path, newmask):
        '''Replace the mask for the watch on path by the new mask. If IN_MASK_ADD is
        set, add the new mask into the existing mask.
        '''
        self._paths[PosixPath(path)].update_mask(newmask)

    def remove(self, path):
        '''Remove watch on the given path.'''
        self._paths[path].remove()
        del self._paths[path]

    def watches(self):
        '''return an iterator of all active watches'''
        return self._path.keys()

    def getmask(self, path):
        '''returns the mask for the watch on the given path'''
        return self._paths[PosixPath(path)].mask

    def close(self):
        'close this watcher instance'
        os.close(self.fd)


class _Watch (object):
    root = PosixPath('/')
    cwd = PosixPath('.')
    parentdir = PosixPath('..')
    
    def __init__(self, watcher, path, mask):
        self.watcher = watcher
        self.path = PosixPath(path)
        self.cwd = PosixPath.cwd()
        self.mask = mask
        self.links = []
        self.watch_complete = False
        self.reconnect()

    def reconnect(self):
        path = _Watch.cwd
        rest = self.path
        if self.links:
            path = self.links[-1].path
            rest = self.links[-1].rest

        linkcount = [0]
        symlinkmax = pathresolver.get_symlinkmax()
        try:
            for path, rest in pathresolver.resolve_symlink(path, rest, set(), {}, linkcount):
                if linkcount[0] > symlinkmax:
                    raise pathresolver.SymlinkLoopError(str(self.path))
                if path == _Watch.cwd:
                    break
                self.add_path_element(path, rest)
        except OSError as e:
            if e.errno in (errno.ENOTDIR, errno.EACCES, errno.ENOENT, errno.ELOOP):
                # Basically any kind of path fault. Mark the reconnect as
                # completed. If this was caused by concurrent filesystem
                # modifications this will be picked up in an inotify event.
                self.watch_complete = True
                return
            else:
                raise
                
        assert path == _Watch.cwd or linkcount[0] > symlinkmax
        self.add_leaf(path)
        self.watch_complete = True

    def add_leaf(self, pth):
        mask = self.mask
        self.links.append(_Link(len(self.links), 'leaf', self, mask, pth, None))
        self.complete_watch = True

    def add_path_element(self, path, rest):
        mask = IN_UNMOUNT | IN_ONLYDIR | IN_EXCL_UNLINK
        assert rest != _Watch.cwd
        if rest.parts[0] == '..':
            mask |= IN_MOVE_SELF | IN_DELETE_SELF
            name = None
        else:
            mask |= IN_MOVE | IN_DELETE | IN_CREATE
            name = rest.parts[0]
        self.links.append(_Link(len(self.links), self, mask, path, name, rest))
        
    def handle_event(self, event, link):
        if self.watch_complete and link.idx == len(self.links) - 1:
            assert event.mask & self.mask
            yield Event(event, str(self.path))
        else:
            for p in self.links[link.idx:]:
                p.remove()
            del self.links[link.idx:]
            self.watch_complete = False
            yield Event(mediumevent(mask=IN_PATH_CHANGED, cookie=0, name=None, wd=event.wd), str(self.path))

    def update_mask(self, newmask):
        if newmask & IN_MASK_ADD:
            self.mask &= newmask
        else:
            self.mask = newmask
        if self.watch_complete:
            oldlink = self.links.pop()
            self.links.append(_Link(len(self.links), self, self.mask,
                                    oldlink.path, None, oldlink.rest))
            oldlink.remove()

    def remove(self):
        for p in self.links:
            p.remove()
        self.watch_complete = False
        del self.links[:]

    def __str__(self):
        return '<_Watch for {}>'.format(str(self.path))
             

mediumevent = namedtuple('mediumevent', 'mask cookie name wd')


class _Link (object):
    def __init__(self, idx, watch, mask, path, name, rest):
        self.idx = idx
        self.watch = watch
        self.mask = mask
        self.path = str(path)
        self.rest = str(rest)
        self.wd = watch.watcher._createwatch(path, name, mask, self.handle_event)

    def handle_event(self, event):
        # This method can be called after the _Link object has been .remove()'d
        # if the underlying event arrived before the remove. So check if this
        # instance is still active.
        if self.wd is None:
            return
        for e in self.watch.handle_event(event, self):
            yield e

    def remove(self):
        self.wd.remove_callback(self.name, self.handle_event)
        self.wd = None

    def _fullname(self):
        if self.name:
            return str(self.path[self.name])
        return str(self.path)

    def __str__(self):
        return '<_Link for {}>'.format(self._fullname())
    

# python 2 compatibility
try:
    basestring
except NameError:
    basestring = str

NoneType = type(None)

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
        assert isinstance(name, (basestring, NoneType))
        assert name is None or not '/' in name
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
        # The list of callbacks can be modified from the handlers, so make a
        # copy.
        for m, c in list(self.callbacks.get(name, ())):
            if not event.mask & m:
                continue
            for e in c(event):
                yield e
        if event.mask & IN_IGNORED:
            assert not self.callbacks
            self.watcher._removewatch(self)
      
    def __str__(self):
        names = ', '.join(c.__self__._fullname() for c in l for l in self.callbacks.values())
        return '<_Descriptor for wd {}: {}>'.format(self.wd, ', '.join(names))


