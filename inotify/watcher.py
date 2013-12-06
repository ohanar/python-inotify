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
from . import event_properties, watch_properties
import array
import errno
import fcntl
import os
import termios



def _make_getter(name, doc):
    def getter(self, mask=constants['IN_' + name.upper()]):
        return self.mask & mask
    getter.__name__ = name
    getter.__doc__ = doc
    return getter



class Event(object):
    '''Derived inotify event class.

    The following fields and properties are available:

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
        'watch',
        )

    @property
    def paths(self):
        if self.watch:
            return list(self.watch.paths)
        return []

    @property
    def fullpath(self):
        pts = self.paths
        if pts:
            p = pts[0]
            if self.name:
                p += '/' + self.name
            return p
        else:
            return None

    @property
    def mask_list(self):
        return constants.decode_mask(self.mask)

    def __init__(self, raw, watch):
        self.raw = raw
        self.watch = watch
        self.mask = raw.mask
        self.cookie = raw.cookie
        self.name = raw.name
    
    def __repr__(self):
        r = repr(self.raw)
        return ('Event(paths={}, ' + r[r.find('(')+1:]).format(repr(self.paths))


for name, doc in event_properties.items():
    setattr(Event, name, property(_make_getter(name, doc), doc=doc))


class _Watch(object):
    '''Represents a watch on a single file.

    The following fields are available:

      wd: The watch descriptor
      paths: A set of paths that this watch watches
      mask: The the mask for this watch
    '''

    __slots__ = (
        'wd',
        'paths',
        'mask',
        '_watcher',
        )

    def __init__(self, parent, wd):
        '''create a new Watch for descriptor wd that is connected to inotify instance
        parent'''
        self._watcher = parent
        self.wd = wd
        self.paths = set()
        self.mask = 0

    def watchno(self):
        '''Return the watch descriptor for this watch'''
        return self.wd

    def _add(self, path, mask):
        '''add another path to this watch, and update the mask'''
        self.paths.add(path)
        self._watcher._paths[path] = self
        if mask & inotify.IN_MASK_ADD:
            self.mask &= (mask & ~inotify.IN_MASK_ADD)
        else:
            self.mask = mask

    def remove_path(self, path):
        '''remove a path from the set of path aliases this watch describes.
        
        If there are no more paths left, this watch will remove itself from the
        inotify instance. The actual removal will only happen once the matching
        IN_IGNORE event is read from the inotify instance.
        '''
        try:
            self.paths.remove(path)
            del self._watcher._paths[path]
            if not self.paths:
                self.remove()
        except KeyError:
            raise InotifyWatcherException(
                '{} does not watch {}'.format(self, path))

    def remove(self):
        '''Schedule this watch to be removed from the inotify instance. The
        actual removal only takes place once the corresponding IN_IGNORE event
        has been received.'''
        self._watcher.remove_watch(self)

    def __repr__(self):
        return '{}.Watch({}, {})'.format(__name__, self._watcher, self.wd)


for name, doc in watch_properties.items():
    setattr(_Watch, name, property(_make_getter(name, doc), doc=doc))



class Watcher(object):
    '''Provide a Pythonic interface to the low-level inotify API.

    Also adds derived information to each event that is not available
    through the normal inotify API, such as directory name.'''

    def __init__(self):
        '''Create a new inotify instance.'''

        self.fd = inotify.init()
        # self._paths is managed from the Watch objects (except when the _Watch
        # object is finally removed).
        self._paths = {}
        self._watches = {}

    def fileno(self):
        '''Return the file descriptor this watcher uses.

        Useful for passing to select and poll.'''

        return self.fd

    def add(self, path, mask):
        '''Add or modify a watch.

        Return the watch descriptor added or modified.'''

        path = os.path.normpath(path)
        # The path may already be watched, so add in the mask.
        wd = inotify.add_watch(self.fd, path, mask | inotify.IN_MASK_ADD)
        if not wd in self._watches:
            self._watches[wd] = _Watch(self, wd)
        watch = self._watches[wd]
        watch._add(path, mask)
        return watch

    def remove_watch(self, watch):
        '''Remove the given watch. The watch is only forgotten from the
        internal datastructures once the corresponding IN_IGNORED event is 
        received from the OS.'''

        inotify.remove_watch(self.fd, watch.wd)

    def remove_path(self, path):
        '''Remove the watch for the given path.'''
        try:
            path = os.path.normpath(path)
            self._paths[path].remove_path(path)
        except KeyError:
            raise InotifyWatcherException("{} is not a watched file".format(path))

    def _remove(self, wd):
        '''Actually remove a watch'''
        try:
            watch = self._watches.pop(wd)
            for path in watch.paths:
                self._paths.pop(path)
        except KeyError:
            raise InotifyWatcherException("watchdescriptor {} not known".format(wd))

    def read(self, block=True):
        '''Read a list of queued inotify events.

        If block is True (the default), block if no events are
        available immediately. Else return an empty list if no events
        are available.'''

        if not len(self._watches):
            raise NoFilesException("There are no files to watch")

        events = []
        for evt in inotify.read(self.fd, block=block):
            watch = None if evt.wd == -1 else self._watches[evt.wd]
            event = Event(evt, watch)
            events.append(event)
            if event.ignored:
                self._remove(event.watch.wd)
        return events

    def __iter__(self):
        while True:
            for e in self.read():
                yield e

    def close(self):
        '''Shut down this watcher.

        All subsequent method calls are likely to raise exceptions.'''

        os.close(self.fd)
        self.fd = None
        self._paths.clear()
        self._watches.clear()

    def num_paths(self):
        '''Return the number of explicitly watched paths.'''
        return len(self._paths)

    def num_watches(self):
        '''Return the number of active watches.'''
        return len(self._watches)

    def watches(self):
        '''Return an iterator of all the watches'''
        return self._watches.values()

    def paths(self):
        '''Return an iterator of all the watched paths.'''
        return self._paths.keys()

    def get_watch(self, path):
        'Return the watch for a given path'
        return self._paths[path]

    def __del__(self):
        if self.fd is not None:
            self.close()

    ignored_errors = [errno.ENOENT, errno.EPERM, errno.ENOTDIR]

    def _add_iter(self, path, mask, onerror=None):
        '''Add or modify watches over path and its subdirectories.

        Yield each added or modified watch descriptor.

        To ensure that this method runs to completion, you must
        iterate over all of its results, even if you do not care what
        they are.  For example:

            for wd in w.add_iter(path, mask):
                pass

        By default, errors are ignored.  If optional arg "onerror" is
        specified, it should be a function; it will be called with one
        argument, an OSError instance.  It can report the error to
        continue with the walk, or raise the exception to abort the
        walk.'''

        # Add the IN_ONLYDIR flag to the event mask, to avoid a possible
        # race when adding a subdirectory.  In the time between the
        # event being queued by the kernel and us processing it, the
        # directory may have been deleted, or replaced with a different
        # kind of entry with the same name.

        submask = mask | inotify.IN_ONLYDIR

        try:
            yield self.add(path, mask)
        except OSError as err:
            if onerror:
                onerror(err)
            else:
                raise
        for root, dirs, names in os.walk(path, topdown=False, onerror=onerror):
            for d in dirs:
                try:
                    yield self.add(root + '/' + d, submask)
                except OSError as err:
                    if err.errno in self.ignored_errors:
                        continue
                    if onerror:
                        onerror(err)
                    else:
                        raise

    def add_all(self, path, mask, onerror=None):
        '''Add or modify watches over path and its subdirectories.

        Return a list of added or modified watch descriptors.

        By default, errors are ignored.  If optional arg "onerror" is
        specified, it should be a function; it will be called with one
        argument, an OSError instance.  It can report the error to
        continue with the walk, or raise the exception to abort the
        walk.'''

        return list(self._add_iter(path, mask, onerror))


class AutoWatcher(Watcher):
    '''Watcher class that automatically watches newly created directories.'''

    def __init__(self, addfilter=None):
        '''Create a new inotify instance.

        This instance will automatically watch newly created
        directories.

        If the optional addfilter parameter is not None, it must be a
        callable that takes one parameter.  It will be called each time
        a directory is about to be automatically watched.  If it returns
        True, the directory will be watched if it still exists,
        otherwise, it will be skipped.'''

        super(AutoWatcher, self).__init__()
        self.addfilter = addfilter

    def read(self, block=False):
        events = super(AutoWatcher, self).read(block)
        for evt in events:
            if evt.mask & inotify.IN_ISDIR and evt.mask & inotify.IN_CREATE:
                if self.addfilter is None or self.addfilter(evt):
                    # See note about race avoidance via IN_ONLYDIR above.
                    mask = evt.watch.mask | inotify.IN_ONLYDIR
                    try:
                        self.add_all(evt.fullpath, mask)
                    except EnvironmentError as err:
                        if err.errno not in self.ignored_errors:
                            raise
        return events


class Threshold(object):
    '''Class that indicates whether a file descriptor has reached a
    threshold of readable bytes available.

    This class is not thread-safe.'''

    __slots__ = (
        'fd',
        'threshold',
        '_iocbuf',
        )

    def __init__(self, fd, threshold=1024):
        self.fd = fd
        self.threshold = threshold
        self._iocbuf = array.array('i', [0])

    def readable(self):
        '''Return the number of bytes readable on this file descriptor.'''

        fcntl.ioctl(self.fd, termios.FIONREAD, self._iocbuf, True)
        return self._iocbuf[0]

    def __call__(self):
        '''Indicate whether the number of readable bytes has met or
        exceeded the threshold.'''

        return self.readable() >= self.threshold



class NoFilesException (Exception):
    '''This inotify instance does not watch anything.'''
    pass
