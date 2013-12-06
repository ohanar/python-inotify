# watcher.py - high-level interfaces to the Linux inotify subsystem

# Copyright 2012-2013 Jan Kanis <jan.code@jankanis.nl>

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

'''High-level interfaces to the Linux inotify subsystem.

The inotify subsystem provides an efficient mechanism for file status
monitoring and change notification.

The PathWatcher class is a wrapper over the low-level inotify
interface. The exposed interface is path based rather than filesystem
inode based such as the low level inotify and the Watcher class
are. This means that if you watch a path /symlink where symlink links
to myfile, PathWatcher will also generate an event if symlink is
removed or changed, where native inotify and the Watcher class will
not.
'''

__author__ = "Jan Kanis <jan.code@jankanis.nl>"

import os, os.path, sys
import errno
import functools
import operator
from collections import namedtuple

from pathlib import PosixPath

from . import pathresolver
from . import inotify as _inotify
from .in_constants import constants, decode_mask, event_properties
from .watcher import NoFilesException, _make_getter
from .pathresolver import SymlinkLoopError, ConcurrentFilesystemModificationError

globals().update(constants)



class Event(object):
    '''
    Derived inotify event class.

    The following fields are available:

    mask: event mask, indicating what kind of event this is

    cookie: rename cookie, if a rename-related event

    path: The path of the watched file/directory

    name: name of the directory entry to which the event occurred. If
    the event is of an IN_PATH_* type, name contains the full path to
    the path element that changed. name may be None if the event did
    not happen to a directory entry.

    raw: The underlying inotify event. In the case of IN_PATH_*
    events, the raw event is constructed and not from the underlying
    inotify system.

    '''

    __slots__ = (
        'cookie',
        'mask',
        'path',
        'name',
        'raw',
        )

    def __init__(self, raw, path):
        self.raw = raw
        self.path = path
        self.mask = raw.mask
        self.cookie = raw.cookie
        self.name = raw.name

    @property
    def mask_list(self):
        return decode_mask(self.mask)

    def __eq__(self, other):
        return isinstance(other, Event) and self.path == other.path and \
            self.mask == other.mask and self.cookie == other.cookie and \
            self.name == other.name and self.raw == other.raw
    
    def __repr__(self):
        r = 'Event(path={}, mask={}'.format(repr(self.path), '|'.join(decode_mask(self.mask)))
        if self.cookie:
            r += ', cookie={}'.format(self.cookie)
        if self.name:
            r += ', name={}'.format(repr(self.name))
        r += ')'
        return r

for name, doc in event_properties.items():
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
        self._pending_watch_removes = 0
        self._reconnect = set()
        self.events = []

    def fileno(self):
        '''Return the file descriptor this watcher uses.  Useful for passing to select
        and poll.
        '''
        return self.fd

    def add(self, path, mask, remember_curdir=None):
        '''Add a watch with the given mask for path. If the path is
        already watched, update the mask according to
        Watcher.update_mask. If remember_curdir is set to True, the
        watch will store the path of the current working directory, so
        that future chdir operations don't change the path. However
        the current path is not watched, so if the current directory
        is moved the meaning of watched paths may change
        undetected. If it is False, relative paths are always resolved
        relative to the working directory at the time of the
        operation.

        Returns the normalized path string, that can be used as key
        for received events.

        '''
        path = PosixPath(path)
        if path in self._paths:
            self._paths[path].update(mask=mask, remember_curdir=remember_curdir)
            return
        self._paths[path] = _PathWatch(self, path, mask, remember_curdir)
        return str(path)

    def _createwatch(self, path, link):
        'create a new _Descriptor for path'
        wd = _inotify.add_watch(self.fd, str(path), link.mask | IN_MASK_ADD)
        if not wd in self._watchdescriptors:
            self._watchdescriptors[wd] = _Descriptor(self, wd)
        desc = self._watchdescriptors[wd]
        desc.register_link(link)
        return desc

    def _removewatch(self, descriptor):
        '''actually remove a descriptor. This should be called after receiving an
        IN_IGNORE event for the descriptor.

        '''
        del self._watchdescriptors[descriptor.wd]
        self._pending_watch_removes -= 1

    def _signal_empty_watch(self, descriptor):
        '''This method is called from a _Descriptor instance if it no longer has any
        callbacks attached to it and so should be deleted. This means
        inotify.read may need to be called again to catch the corresponding
        IN_IGNORE event.
        '''
        if descriptor.active:
            _inotify.remove_watch(self.fd, descriptor.wd)
        self._pending_watch_removes += 1

    def _reconnect_required(self, watch):
        '''Register a watch to be .reconnect()'ed after event processing is finished'''
        self._reconnect.add(watch)

    def read(self, block=True):
        '''Read a list of queued inotify events.

        block: If block is false, return only those events that can be
        read immediately.
        '''

        # We call _inotify.read once at first. If we are expecting an
        # IN_IGNORE, we read it again until we get all pening
        # IN_IGNOREs. Secondly, if a
        # ConcurrentFilesystemModificationError is thrown in
        # pathresolver, that may be because other path elements have
        # changed and there are still unread events indicating
        # this. So if after reconnecting there are again _PathWatches
        # that need reconnecting, we also call _inotify.read
        # again. Continue this loop until there are no more pending
        # IN_IGNORE events and no more _PathWatches awaiting
        # reconnection.
        if self.events:
            e, self.events = self.events, []
            return e
        self._do_reconnect()

        if not len(self._watchdescriptors):
            raise NoFilesException("There are no files to watch")

        lastevent = None
        do1 = True
        while do1 or self._pending_watch_removes > 0 or self._reconnect:
            do1 = False
            do2 = True
            while do2 or self._pending_watch_removes > 0:
                do2 = False
                for e in self._read_events(block):
                    # only filter duplicate path_changed events to
                    # prevent hiding of bugs that may cause duplicate
                    # other events to be created.
                    if e.path_changed and e == lastevent:
                        continue
                    self.events.append(e)
                    lastevent = e
            self._do_reconnect()
        events, self.events = self.events, []
        return events

    def _read_events(self, block):
        for evt in _inotify.read(self.fd, block=block):
            if evt.wd == -1:
                eventiter = self._handle_descriptorless_event(evt)
            else:
                eventiter = self._watchdescriptors[evt.wd].handle_event(evt)
            for e in eventiter:
                yield e

    def _do_reconnect(self):
        # Do not just clear the list, but replace it, because the
        # reconnect call can cause new pathwatches to require
        # recursive reconnection.
        r, self._reconnect = self._reconnect, set()
        for w in r:
            w.reconnect()

    def _handle_descriptorless_event(self, evt):
        event = Event(evt, None)
        if event.q_overflow:
            for w in self._paths.values():
                w._queue_overflow()
        yield event

    def update(self, path, newmask=0, remember_curdir=None):
        '''Replace the mask for the watch on path by the new mask. If
        IN_MASK_ADD is set, add the new mask into the existing
        mask. If remember_curdir is set to True, save the current
        working directory in the watch.
        '''
        self._paths[PosixPath(path)].update(newmask, remember_curdir)

    def remove(self, path):
        '''Remove watch on the given path.'''
        path = PosixPath(path)
        self._paths[path].remove()
        del self._paths[path]

    def watches(self):
        '''return an iterator of all active watches'''
        return [str(p) for p in self._paths.keys()]

    def getmask(self, path):
        '''returns the mask for the watch on the given path'''
        return self._paths[PosixPath(path)].mask

    def close(self):
        'close this watcher instance'
        if self.fd is None:
            return
        os.close(self.fd)
        self._watchdescriptors.clear()
        self._paths.clear()
        self.fd = None

    def __del__(self):
        if self.fd is not None:
            self.close()
            

syntheticevent = namedtuple('syntheticevent', 'mask cookie name wd')

class _PathWatch (object):
    root = PosixPath('/')
    curdir = PosixPath('.')
    parentdir = PosixPath('..')
    
    def __init__(self, watcher, path, mask, remember_curdir=None):
        self.watcher = watcher
        self.path = path
        self.mask = mask
        self.links = []
        # watch_complete values:
        # 0: reconnect needed
        # 1: no reconnect needed, but the final target is not being
        # watched (e.g. due to symlink loops)
        # 2: The path is fully resolved and the final target is being
        # watched.
        self.watch_complete = 0
        self._update_curdir(True if remember_curdir is None else remember_curdir)
        self.reconnect()

    def _update_curdir(self, remember_curdir):
        if remember_curdir is True:
            self.cwd = PosixPath.cwd()
        elif remember_curdir is False:
            self.cwd = _PathWatch.curdir
         
    def reconnect(self):
        assert self.watch_complete == 0
        path = self.cwd
        rest = self.path
        linkcount = [0]
        if self.links:
            path = PosixPath(self.links[-1].path)
            rest = PosixPath(self.links[-1].rest)
            linkcount[0] = self.links[-1].linkcount

        symlinkmax = pathresolver.get_symlinkmax()
        try:
            pathsiter = pathresolver.resolve_symlink(path, rest, set(), {}, linkcount)
            if self.links:
                # The first yielded path pair is the one the last link is watching.
                next(pathsiter)
            for path, rest in pathsiter:
                if linkcount[0] > symlinkmax:
                    raise pathresolver.SymlinkLoopError(str(self.path))
                if rest == _PathWatch.curdir:
                    break
                self.add_path_element(path, rest, linkcount[0])
        except ConcurrentFilesystemModificationError:
            # leave watch_complete at 0, we may need to read more events first
            return
        except OSError as e:
            if e.errno in (errno.ENOTDIR, errno.EACCES, errno.ENOENT, errno.ELOOP):
                # Basically any kind of path fault. Mark the reconnect as
                # completed. If this was caused by concurrent filesystem
                # modifications this will be picked up in an inotify event.
                self.watch_complete = 1
                return
            else:
                raise
                
        assert rest == _PathWatch.curdir
        self.add_leaf(path)
        self.watch_complete = 2

    def add_leaf(self, path):
        self.links.append(_Link(len(self.links), self, self.mask, path, None, _PathWatch.curdir, None))

    def add_path_element(self, path, rest, linkcount):
        assert rest != _PathWatch.curdir
        mask = IN_UNMOUNT | IN_ONLYDIR | IN_EXCL_UNLINK | IN_IGNORED
        if rest.parts[0] == '..':
            mask |= IN_MOVE_SELF | IN_DELETE_SELF
            name = None
        else:
            mask |= IN_MOVE | IN_DELETE | IN_CREATE
            name = rest.parts[0]
        self.links.append(_Link(len(self.links), self, mask, path, name, rest, linkcount))
        
    _eventmap = {IN_MOVED_FROM | IN_MOVE_SELF: IN_PATH_MOVED_FROM,
                 IN_MOVED_TO: IN_PATH_MOVED_TO,
                 IN_DELETE | IN_DELETE_SELF | IN_IGNORED: IN_PATH_DELETE,
                 IN_CREATE: IN_PATH_CREATE,
                 IN_UNMOUNT: IN_PATH_UNMOUNT,
                }
    def handle_event(self, event, link):
        if self.watch_complete == 2 and link.idx == len(self.links) - 1:
            assert event.mask & (self.mask | IN_IGNORED)
            if event.mask & IN_IGNORED:
                self._poplinks_from(link.idx)
                if event.mask & IN_UNMOUNT:
                    self._register_reconnect()
            if event.mask & self.mask:
                yield Event(event, str(self.path))
        else:
            i = link.idx
            if event.mask & (IN_MOVE | IN_DELETE | IN_CREATE):
                # something happened to a directory entry
                i += 1
            self._poplinks_from(i)
            if event.mask & (IN_MOVED_TO|IN_CREATE|IN_UNMOUNT):
                self._register_reconnect()
            if not event.mask & (IN_MOVE_SELF | IN_DELETE_SELF | IN_IGNORED | IN_UNMOUNT):
                name = str(PosixPath(link.path)[link.name])
            else:
                name = link.path
            for m, t in _PathWatch._eventmap.items():
                if event.mask & m:
                    evttype = t
            evttype |= (event.mask & IN_ISDIR)
            yield Event(syntheticevent(mask=evttype, cookie=0, name=name, wd=event.wd), str(self.path))

    def _poplinks_from(self, startidx):
        if startidx >= len(self.links):
            return
        self.watch_complete = min(1, self.watch_complete)
        for p in self.links[startidx:]:
            p.remove()
        del self.links[startidx:]

    def _register_reconnect(self):
        self.watch_complete = 0
        self.watcher._reconnect_required(self)

    def _queue_overflow(self):
        self._poplinks_from(1)
        self._register_reconnect()

    def update(self, newmask=0, remember_curdir=None):
        self._update_curdir(remember_curdir)
        if not newmask:
            return
        if newmask & IN_MASK_ADD:
            self.mask &= newmask
        else:
            self.mask = newmask
        if self.watch_complete == 2:
            oldlink = self.links.pop()
            self.add_leaf(oldlink.path)
            oldlink.remove()

    def remove(self):
        self._poplinks_from(0)

    def __repr__(self):
        return '<_PathWatch for {}>'.format(str(self.path))


class _Link (object):

    __slots__ = ('idx',
                 'watch',
                 'mask',
                 'path',
                 'name',
                 'rest',
                 'linkcount',
                 'wd',
                )

    def __init__(self, idx, watch, mask, path, name, rest, linkcount):
        self.idx = idx
        self.watch = watch
        self.mask = mask
        self.path = str(path)
        self.name = name
        self.rest = str(rest)
        self.linkcount = linkcount
        assert isinstance(self.name, (basestring, NoneType))
        assert name is None or not '/' in name
        self.wd = watch.watcher._createwatch(path, self)

    def handle_event(self, event):
        # This method can be called after the _Link object has been .remove()'d
        # if the underlying event arrived before the remove. So check if this
        # instance is still active.
        if self.wd is None:
            return
        for e in self.watch.handle_event(event, self):
            yield e

    def remove(self):
        self.wd.remove_link(self)
        self.wd = None

    def printname(self):
        return self.path+':'+str(PosixPath(self.rest).parts[0:1])

    def __repr__(self):
        return '<_Link for {}>'.format(self.printname())
    

# python 2 compatibility
try:
    basestring
except NameError:
    basestring = str

NoneType = type(None)

class _Descriptor (object):

    __slots__ = ('watcher',
                 'wd',
                 'mask',
                 'callbacks',
                 'active',
                )

    def __init__(self, watcher, wd):
        self.watcher = watcher
        self.wd = wd
        self.mask = 0
        self.active = True
        # callbacks is indexed by name to improve speed and because we
        # can. Indexing by name and mask would be faster but would be more
        # cumbersome to implement.
        self.callbacks = {}

    def register_link(self, link):
        assert self.active
        self.mask |= link.mask
        self.callbacks.setdefault(link.name, []).append(link)

    def remove_link(self, link):
        self.callbacks[link.name].remove(link)
        if not self.callbacks[link.name]:
            del self.callbacks[link.name]
        if not self.callbacks:
            self.watcher._signal_empty_watch(self)

    def handle_event(self, event):
        if event.mask & IN_IGNORED:
            self.active = False
        # The list of callbacks can be modified from the handlers, so make a
        # copy.
        callbacks = list(self.callbacks.get(event.name, ()))
        if event.name != None:
            callbacks.extend(self.callbacks.get(None, ()))
        for l in callbacks:
            if not event.mask & (l.mask | IN_IGNORED):
                continue
            for e in l.handle_event(event):
                yield e
        if event.mask & IN_IGNORED:
            assert not self.callbacks
            assert not self.active
            self.watcher._removewatch(self)
        
    def __repr__(self):
        names = ', '.join(set(l.printname() for lst in self.callbacks.values() for l in lst))
        return '<_Descriptor for wd {}: {}>'.format(self.wd, names)


