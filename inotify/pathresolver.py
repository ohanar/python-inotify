# pathresolver.py - This module contains an iterator that iterates over all
# elements of a path including any symlinks. 

# Copyright 2012-2013 Jan Kanis <jan.code@jankanis.nl>

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.


__author__ = "Jan Kanis <jan.code@jankanis.nl>"


import os, errno
import tempfile, shutil
from pathlib import PosixPath


_curdir = PosixPath('.')
_root = PosixPath('/')
_parentdir = PosixPath('..')


def resolve_path(path):
    '''Resolve the symlinks in path, yielding all filesystem locations that are traversed.

    The yielded value is a tuple, of which the first element is a symlink-free
    path, and the second element is a path relative to the first element that
    has not yet been traversed. This second element may contain more symlinks.
    
    The resolution implementation will follow an unbounded number of symlinks
    but will still detect symlink loops if they prevent a path from resolving.

    path can be given as a string or as a pathlib object. The yielded values
    are pathlib.PosixPath objects.

    '''
    linkcache = {}
    linkcounter = [0]
    for p in resolve_symlink(_curdir, PosixPath(path), set(),
                                  linkcache, linkcounter):
        yield p


def resolve_symlink(location, link_contents, active_links, known_links, linkcounter):
    '''Recursively resolve a symlink to the file or directory it ultimately points
    to. This function handles an unlimited number of symlinks, and
    correctly detects symlink loops. All path parameters should be given as
    pathlib.PosixPath instances.

    location: The directory in which the currently to be resolved link resides.

    link_contents: The path stored in the symlink as returned by readlink().

    active_links: a set of symlinks that is currently being resolved.

    linkcache: a dictionary of link location -> resolved target paths. This
    cache prevents this function from having to resolve the same symlink
    twice. (Note that having to traverse the same symlink multiple times
    does not necessarily mean that the path does not resolve to anything.)

    linkcounter: A list containing a single number. (We use a list so that the
    value can be passed by reference.) This number is updated to indicate the
    total number of symlinks that has been traversed.

    '''

    while True:
        if link_contents.is_absolute():
            location = _root
            link_contents = link_contents.relative()

        yield location, link_contents
        if link_contents == _curdir:
            return

        if link_contents.parts[0] == '..':
            # We need to choose here if we allow traversing of a path above
            # the root or above the current directory. Going above CWD
            # should be allowed as long as we don't go above / by doing
            # so. The OS allows going to /.. (which just ends up at /
            # again), so for consistency with that we also allow it,
            # although a path that requires us to do this is probably a bug
            # somewhere.
            if all(p in ('/', '..') for p in location.parts):
                location = location['..']
            else:
                location = location.parent()
            # Strip the first part of link_contents off
            link_contents = link_contents.parts[1:]
            continue

        try:
            nextpath = location[link_contents.parts[0]]
            newlink = PosixPath(os.readlink(str(nextpath)))
        except OSError as e:
            if e.errno == errno.EINVAL:
                # The entry is not a symbolic link, assume it is a normal file
                # or directory. If it is a file and we need it to be a
                # directory that will be detected the next time through the
                # loop in the os.errno.ENOTDIR check. Checking it here would be
                # possible, but keeping the number of system calls at one per
                # loop makes reasoning about race conditions easier.
                location = nextpath
                link_contents = link_contents.parts[1:]
                continue
            if e.errno == errno.ENOENT:
                # The entry does not exist
                raise FileNotFoundError(nextpath)
            if e.errno == errno.ENOTDIR:
                # At this point we know the path is not valid, but we can not
                # determine with certainty what is wrong. If there were no
                # concurrent modifications we can safely make an is_dir()
                # call. If concurrent modifications did happen the is_dir()
                # check may succeed erroneously but we can't detect all
                # concurrent modifications anyway. If the check fails
                # (erroneously or not) that indicates a concurrent modification
                # so we fall through.
                if not location.is_dir():
                    raise NotADirectoryError(location)
                # We should not be able to get here, unless there is a bug
                # or some relevant part of the file system was changed
                # concurrently while we were resolving this link.
                raise ConcurrentFilesystemModificationError(nextpath)
            if e.errno == errno.ELOOP:
                # Can only happen if a path component was changed concurrently
                raise ConcurrentFilesystemModificationError(nextpath)
            # For other OSErrors (such as in case of EACCESS) we propagate to
            # the caller. 
            raise

        # It is a symlink!
        if nextpath in active_links:
            raise SymlinkLoopError(nextpath)

        link_contents = link_contents.parts[1:]
        # We have not yet attempted traversing this symlink during the
        # current call or any of its parents.
        if nextpath in known_links:
            # known_links stores fully resolved paths, so we don't need to
            # traverse the cached path and can just continue our traversal from
            # there.
            location = known_links[nextpath]
            continue
        
        # An unknown link, resolve it recursively
        linkcounter[0] += 1
        # Don't yield the very last result of this recursive call immediately,
        # we still want to process that further. 
        lastloc, lastlink = None, None
        for loc, link in resolve_symlink(location, newlink,
                          active_links.union((nextpath,)), known_links, linkcounter):
            if lastloc:
                yield lastloc, lastlink[link_contents]
            lastloc, lastlink = loc, link
        # The last yielded location is the final resolution of the symlink. The
        # last yielded link_contents is always '.' so we can ignore that.
        known_links[nextpath] = loc
        location = loc
        continue


_symlinkmax = None
def get_symlinkmax():
  '''Returns the maximum number of symlinks that this system will traverse in
  resolving a file path.

  '''
  global _symlinkmax
  if not _symlinkmax is None:
    return _symlinkmax

  try:
    tempdir = tempfile.mkdtemp(prefix='inotify-symlinkmax-tmpdir-')
    open(tempdir+'/testfile', 'w').close()

    target = 'testfile'
    for i in range(1, 60):
      name = 'l'+str(i)
      os.symlink(target, tempdir+'/'+name)
      target = name

      try:
        open(tempdir+'/'+name).close()
      except OSError as e:
        if e.errno == errno.ELOOP:
          _symlinkmax = i - 1
          break
        raise
    
  finally:
    if tempdir:
      shutil.rmtree(tempdir)
  return _symlinkmax



class InvalidPathError (OSError):
    def __init__(self, msg, path, *args):
        self.filename = path
        OSError.__init__(self, msg, *args)

class SymlinkLoopError (InvalidPathError):
    def __init__(self, path, *args):
        msg = "Path not valid: The symlink at '{}' forms a symlink loop".format(path)
        InvalidPathError.__init__(self, msg, path, errno.ELOOP, *args)

class ConcurrentFilesystemModificationError (InvalidPathError):
    def __init__(self, path, *args):
        msg = "Path not valid: A concurrent change was detected while traversing '{}'".format(path)
        InvalidPathError.__init__(self, msg, path, *args)


# To be Python 2 and 3 compatible and also inherit from the right exception
# types in python 3, we need some boilerplate.

fnf_msg = "Path not valid: '{}' does not exist"
nad_msg = "Path not valid: '{}' is not a directory"

if sys.version_info[0] >= 3:
    class FileNotFoundError (InvalidPathError, FileNotFoundError):
        def __init__(self, path, *args):
            InvalidPathError.__init__(self, fnf_msg.format(path), path,
                                          errno.ENOENT, *args)

    class NotADirectoryError (InvalidPathError, NotADirectoryError):
        def __init__(self, path, *args):
            InvalidPathError.__init__(self, nad_msg.format(path), path,
                                          errno.ENOTDIR, *args)

else:
    class FileNotFoundError (InvalidPathError):
        def fnf__init__(self, path, *args):
            InvalidPathError.__init__(self, fnf_msg.format(path), path,
                                          errno.ENOENT, *args)

    class NotADirectoryError (InvalidPathError):
        def __init__(self, path, *args):
            InvalidPathError.__init__(self, nad_msg.format(path), path,
                                          errno.ENOTDIR, *args)




