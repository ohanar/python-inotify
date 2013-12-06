#!/usr/bin/env py.test

# This testing script can be run either from python 3 or python 2. Run with
# `py.test test.py` or `py.test-2.7 test.py`.
#
# This script will try to import the inotify module from the build directory in
# ../build/lib.linux-{platform}-{pyversion}/inotify relative to its own
# location. If that directory cannot be found it will import the inotify module
# from the default path.


# from __future__ import print_function

import sys, os, errno, shutil, tempfile, itertools, functools, operator
import pytest
from pathlib import PosixPath as P

if not sys.platform.startswith('linux'): raise Exception("This module will only work on Linux")

# Find the package to test. We first try an inotify in the current directory,
# then try to find one in the build directory of this package, and else we
# import from the default path.
un = os.uname()
ver = '.'.join(str(x) for x in sys.version_info[:2])
testdir = os.path.dirname(os.path.abspath(__file__))
inotify_dir = os.path.normpath(testdir + '/../build/lib.{sys}-{plat}-{ver}/'.format(
    sys=un[0].lower(), plat=un[4], ver=ver))
idx = None
if os.path.exists(inotify_dir+'/inotify') and not inotify_dir in sys.path:
  # Insert at the beginning of sys.path, but not before the current directory
  # as we do not want to override an explicit inotify package in the current
  # directory.
  try:
    idx = next(i for i, p in enumerate(sys.path) if p and os.path.samefile(p, '.'))
  except StopIteration:
    # In interactive mode, there is no entry for the current directory, but the
    # first entry of sys.path is the empty string which is interpreted as
    # current directory. So if a path to the current directory is not found,
    # insert after this first empty string.
    idx = 0
  sys.path.insert(idx + 1, inotify_dir)
del un, ver, testdir, idx


import inotify

globals().update(inotify.constants)

print("\nTesting inotify module from", inotify.__file__)


"""
more needed concurrency tests:

- remember_curdir==False and current directory is deleted

- remember_curdir==True and current directory is deleted

- path element directory foo is deleted and replaced with a file, but
  the creation event has not been read yet. Make sure no infinite loop
  occurs due to ConcurrentFilesystemModificationError, and also make
  sure we wait for the creation event and restore the watch.

- Can inotify.read be interrupted without losing events or losing
  consistency?
  ----> only a blocking read can now safely be interrupted, but there
  should be no specific need to interrupt a non-blocking read.

- Unmounting of a filesystem mounted on a watched directory only
  generates events on the mount point itself, not on the parent
  directory.

- Worse than the above, mounting a new filesystem on a watched
  directory does not generate any events of any kind whatsoever, so in
  that case it seems to be impossible to maintain a fully watched
  path. To catch mounts we could use a netlink socket and watch for
  block device changes, but bind mounts don't even generate those.

- The best way to catch mounts appears to be to monitor /dev/mtab. Not
  100% reliable but it probably works for most cases.

"""


# from IPython import embed as ipythonembed


@pytest.fixture(autouse=True)
def preparedir(request):
  # global tempdir
  tempdir = tempfile.mkdtemp(prefix='inotify-test-tmpdir-')
  request.addfinalizer(lambda tempdir=tempdir: shutil.rmtree(tempdir))
  os.chdir(tempdir)
  open('testfile', 'w').close()
  os.mkdir('testdir')


@pytest.fixture(scope='module')
def symlinkmax():
  symlinkmax = pathresolver.get_symlinkmax()
  print('\ndetected system SYMLINKMAX:', symlinkmax)
  return symlinkmax

def makelinkchain(target, directory, numlinks):
  for i in range(1, numlinks+1):
    name = 'l'+str(i)
    os.symlink(target, 'directory/'+name)
    target = name


@pytest.fixture
def w(request):
  w = inotify.PathWatcher()
  request.addfinalizer(lambda w=w: w.close())
  return w


def test_constants():
  assert IN_PATH_MOVED_TO > inotify.inotify.IN_ALL_EVENTS
  c_events = functools.reduce(operator.or_, (v for k,v in inotify.inotify.__dict__.items() if k.startswith('IN_')))
  path_events = functools.reduce(operator.or_, (v for k,v in inotify.constants.items() if k.startswith('IN_PATH_')))
  assert not c_events & path_events
  assert path_events > c_events

def test_repr(w):
  w.add('.', IN_ALL_EVENTS)
  repr(w)
  repr(w._paths)
  repr(w._watchdescriptors)
  repr(next(iter(w._paths.values())).links)

def test_open(w):
  mask = IN_OPEN | IN_CLOSE
  w.add('testfile', mask)
  watch = w._paths[P('testfile')]

  assert len(watch.links) == 2
  assert watch.path == P('testfile')
  assert watch.watcher == w
  assert watch.mask == mask

  link1 = watch.links[0]
  assert link1.idx == 0
  assert link1.path == str(P.cwd())
  assert link1.rest == 'testfile'
  assert link1.mask == IN_UNMOUNT | IN_ONLYDIR | IN_EXCL_UNLINK | IN_IGNORED | IN_MOVE | IN_DELETE | IN_CREATE
  assert link1.watch == watch
  wd = link1.wd
  assert wd.callbacks['testfile'] == [link1]
  assert wd.mask == link1.mask
  assert wd.watcher == w
  watchdesc = wd.wd
  assert w._watchdescriptors[watchdesc] == wd

  link2 = watch.links[1]
  assert link2.idx == 1
  assert link2.path == str(P.cwd()['testfile'])
  assert link2.rest == '.'
  assert link2.mask == IN_OPEN | IN_CLOSE
  assert link2.watch == watch
  wd = link2.wd
  assert wd.callbacks[None] == [link2]
  assert wd.mask == link2.mask
  assert wd.watcher == w
  watchdesc = wd.wd
  assert w._watchdescriptors[watchdesc] == wd
  
  open('testfile').close()
  evts = w.read(block=False)
  ev1, ev2 = evts
  assert ev1.open
  assert ev2.close
  assert ev2.close_nowrite

  os.remove('testfile')
  ev3 = w.read(block=False)[0]
  assert ev3.path_delete and ev3.path_changed
  assert ev3.path == 'testfile'
  assert P(ev3.name).parts[-1] == 'testfile'

  w.close()


def test_linkchange(w):
  os.symlink('testfile', 'link3')
  os.symlink('link3', 'link2')
  os.symlink('link2', 'link1')
  w.add('link1', inotify.IN_OPEN, remember_curdir=False)
  watch = w._paths[P('link1')]
  assert len(watch.links) == 5
  w1, w2, w3, w4, wt  = watch.links
  assert [wx.name for wx in (w1, w2, w3, w4)] == 'link1 link2 link3 testfile'.split()
  assert (wt.path, wt.name) == ('testfile', None)
  assert w1.wd == w2.wd == w3.wd == w4.wd
  desc = w1.wd
  linkmask = IN_UNMOUNT | IN_ONLYDIR | IN_EXCL_UNLINK | IN_IGNORED | IN_MOVE | IN_DELETE | IN_CREATE
  for p, l in {'link1':w1, 'link2':w2, 'link3':w3}.items():
    assert desc.callbacks[p] == [l]
    assert l.mask == linkmask

  os.rename('link2', 'link2new')
  e = w.read()
  assert len(e) == 1
  e1 = e[0]
  assert e1.path_changed
  assert e1.path_moved
  assert len(w._watchdescriptors) == 1
  assert len(watch.links) == 2
  assert len(list(itertools.chain(*watch.links[0].wd.callbacks.values()))) == 2

  os.rename('link1', 'link1new')
  e = w.read()
  assert len(e) == 1
  e1 = e[0]
  assert e1.path_changed
  assert e1.path_moved
  assert len(w._watchdescriptors) == 1
  assert len(watch.links) == 1

  os.rename('link2new', 'link1')
  e = w.read()
  assert len(e) == 1
  e1 = e[0]
  assert e1.path_moved
  assert len(watch.links) == 4


def test_multi(w):
  open('file2', 'w').close()
  os.symlink('file2', 'link2')
  os.symlink(str(P.cwd()['link2']), 'link3')
  os.symlink('testfile', 'link4')
  
  w.add('link3', IN_OPEN)
  w.add('link4', IN_OPEN)

  open('file2').close()
  evts = w.read()
  assert len(evts) == 1
  e = evts[0]
  assert e.open
  assert e.path == 'link3'

  open('testfile').close()
  evts = w.read()
  assert len(evts) == 1
  e = evts[0]
  assert e.open
  assert e.path == 'link4'

  assert len(w._watchdescriptors) == 5

  os.remove('link3')
  os.symlink('link4', 'link3')

  evts = w.read()
  open('testfile').close()
  evts.extend(w.read())
  assert len(evts) == 4
  

def test_move(w):
  w.add('.', IN_MOVE)
  assert w.read(0) == []
  os.rename('testfile', 'targetfile')
  ev = w.read()
  for e in ev:
    if e.name == 'testfile':
      assert e.moved_from
    if e.name == 'targetfile':
      assert e.moved_to
  assert ev[0].cookie and ev[0].cookie == ev[1].cookie


def test_alias(w):
  '''The inotify system maps watch requests to aliases (e.g. symlinks) to the
  same watch descriptor, so we need to be sure that a watch is only really
  removed if all paths it is watching are dismissed.'''

  os.symlink('testfile', 'testlink')
  w1 = w.add('testfile', inotify.IN_OPEN)
  w2 = w.add('testlink', inotify.IN_OPEN)
  assert set(w.watches()) == {'testfile', 'testlink'}
  open('testlink').close()
  ev = w.read(0)
  assert len(ev) == 2
  w.remove('testfile')
  open('testlink').close()
  ev = w.read(0)
  assert len(ev) == 1


def test_delete(w):
    w.add('testfile', IN_DELETE_SELF | IN_IGNORED)
    os.remove('testfile')
    evts = w.read(0)
    ev1, ev2, ev3 = evts
    assert ev1.delete_self
    assert ev2.ignored
    assert ev3.path_delete
    assert len(w.watches()) == 1

def test_wrongpath(w):
    w.add('nonexistant', IN_OPEN)
    assert w.read(block=False) == []
    open('nonexistant', 'w').close()
    evts = w.read()
    ev1 = evts[0]
    assert ev1.path_create


