#!/usr/bin/env py.test

# This testing script can be run either from python 3 or python 2. Run with
# `py.test test.py` or `py.test-2.7 test.py`.
#
# This script will try to import the inotify module from the build directory in
# ../build/lib.linux-{platform}-{pyversion}/inotify relative to its own
# location. If that directory cannot be found it will import the inotify module
# from the default path.


# from __future__ import print_function

import sys, os, shutil, tempfile, inspect
import pytest

if not sys.platform.startswith('linux'): raise Exception("This module will only work on Linux")

# # find the build dir
# un = os.uname()
# ver = '.'.join(str(x) for x in sys.version_info[:2])
# testdir = os.path.dirname(os.path.abspath(__file__))
# inotify_dir = os.path.normpath(testdir + '/../build/lib.{sys}-{plat}-{ver}/'.format(
#     sys=un[0].lower(), plat=un[4], ver=ver))
# if os.path.exists(inotify_dir+'/inotify') and not inotify_dir in sys.path:
#   sys.path[:0] = [inotify_dir]
# del un, ver, testdir

import inotify
from inotify import newwatcher as watcher

print("\nTesting inotify module from", inotify.__file__)



@pytest.fixture(autouse=True)
def preparedir(request):
  # global tempdir
  tempdir = tempfile.mkdtemp(prefix='inotify-test-tmpdir-')
  request.addfinalizer(lambda tempdir=tempdir: shutil.rmtree(tempdir))
  os.chdir(tempdir)
  open('testfile', 'w').close()
  os.mkdir('testdir')


@pytest.fixture
def w():
  return watcher.Watcher()


def test_open(w):
  mask = inotify.IN_OPEN | inotify.IN_CLOSE
  w.add('testfile', mask)
  watch = w._paths['testfile']

  assert len(watch.links) == 1
  assert watch.path == ['testfile']
  assert watch.watcher == w
  st = os.stat('testfile')
  # assert watch.inode == (st.st_dev, st.st_ino)
  assert watch.mask == mask
  link = watch.links[0]
  assert link.idx == 0
  assert link.path == 'testfile'
  linkmask = mask | inotify.IN_MOVE_SELF | inotify.IN_DELETE_SELF
  assert link.mask == linkmask
  assert link.watch == watch
  wd = link.wd
  assert wd.callbacks == [(linkmask, None, link.handle_event)]
  assert wd.mask == linkmask
  assert wd.watcher == w
  watchdesc = wd.wd
  assert w._watchdescriptors[watchdesc] == wd
  assert w._paths['testfile'] == watch
  
  open('testfile').close()
  ev1, ev2 = w.read(block=False)
  assert ev1.open
  assert ev2.close
  assert ev2.close_nowrite
  w.close()


def test_linkchange(w):
  os.symlink('testfile', 'link1')
  os.symlink('link1', 'link2')
  os.symlink('link2', 'link3')
  w.add('link3', inotify.IN_OPEN)
  watch = w._paths['link3']
  assert len(watch.links) == 4
  w1, w2, w3, w4 = w.links
  # for w in [w1,w2,w3]:
  #     assert
  global W; W = w
  # import pdb; pdb.set_trace()

# def test_move(w):
#   w.add('.', inotify.IN_MOVE)
#   assert w.read(0) == []
#   os.rename('testfile', 'targetfile')
#   ev = w.read(0)
#   for e in ev:
#     if e.name == 'testfile':
#       assert e.moved_from
#     if e.name == 'targetfile':
#       assert e.moved_to
#   assert ev[0].cookie and ev[0].cookie == ev[1].cookie


# def test_alias(w):
#   '''The inotify system maps watch requests to aliases (e.g. symlinks) to the
#   same watch descriptor, so we need to be sure that a watch is only really
#   removed if all paths it is watching are dismissed.'''

#   os.symlink('testfile', 'testlink')
#   w1 = w.add('testfile', inotify.IN_OPEN)
#   w2 = w.add('testlink', inotify.IN_OPEN)
#   assert w1 == w2
#   assert set(w.paths()) == {'testfile', 'testlink'}
#   assert w.get_watch('testfile') == w.get_watch('testlink')
#   assert len(w.watches()) == 1
#   open('testlink').close()
#   ev = w.read(0)
#   assert len(ev) == 1
#   w.remove_path('testfile')
#   open('testlink').close()
#   ev = w.read(0)
#   assert len(ev) == 1


# def test_delete(w):
#     w.add('testfile', inotify.IN_DELETE_SELF)
#     os.remove('testfile')
#     ev1, ev2 = w.read(0)
#     assert ev1.delete_self
#     assert ev2.ignored
#     assert w.num_watches() == 0

# def test_wrongpath(w):
#     with pytest.raises(OSError) as excinfo:
#         w.add('nonexistant', inotify.IN_OPEN)
#     assert excinfo.value.errno == os.errno.ENOENT
#     with pytest.raises(OSError) as excinfo:
#         w.add_all('nonexistant', inotify.IN_OPEN)
#     assert excinfo.value.errno == os.errno.ENOENT
