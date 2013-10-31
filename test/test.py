#!/usr/bin/env py.test

# This testing script can be run either from python 3 or python 2. Run with
# `py.test test.py` or `py.test-2.7 test.py`.
#
# If run from the test directory 


from __future__ import print_function

import sys, os, shutil, tempfile, inspect
import pytest

if not sys.platform.startswith('linux'): raise Exception("This module will only work on Linux")

# find the build dir
un = os.uname()
ver = '.'.join(str(x) for x in sys.version_info[:2])
testdir = os.path.dirname(os.path.abspath(__file__))
dir = os.path.normpath(testdir + '/../build/lib.{sys}-{plat}-{ver}/'.format(
    sys=un[0].lower(), plat=un[4], ver=ver))
if os.path.exists(dir+'/inotify') and not dir in sys.path:
  sys.path[:0] = [dir]
del un, ver, testdir, dir

import inotify
from inotify import watcher

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
  w.add('testfile', inotify.IN_OPEN | inotify.IN_CLOSE)
  open('testfile').close()
  ev1, ev2 = w.read(0)
  assert ev1.open
  assert ev2.close
  assert ev2.close_nowrite
  w.close()


def test_move(w):
  w.add('.', inotify.IN_MOVE)
  assert w.read(0) == []
  os.rename('testfile', 'targetfile')
  ev = w.read(0)
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
  assert w1 == w2
  assert set(p for p, _ in w.paths()) == {'testfile', 'testlink'}
  assert w.get_watch('testfile') == w.get_watch('testlink')
  assert len(w.watches()) == 1
  open('testlink').close()
  ev = w.read(0)
  assert len(ev) == 1
  w.remove_path('testfile')
  open('testlink').close()
  ev = w.read(0)
  assert len(ev) == 1


