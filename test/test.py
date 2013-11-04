#!/usr/bin/env py.test

# This testing script can be run either from python 3 or python 2. Run with
# `py.test test.py` or `py.test-2.7 test.py`.
#
# This script will try to import the inotify module from the build directory in
# ../build/lib.linux-{platform}-{pyversion}/inotify relative to its own
# location. If that directory cannot be found it will import the inotify module
# from the default path.


from __future__ import print_function

import sys, os, shutil, tempfile, inspect
import pytest

if not sys.platform.startswith('linux'): raise Exception("This module will only work on Linux")

# find the build dir
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
  w2 = w.add('testlink', inotify.IN_CLOSE)
  assert w1 == w2
  assert set(w.paths()) == {'testfile', 'testlink'}
  assert w.get_watch('testfile') == w.get_watch('testlink')
  assert len(w.watches()) == 1
  open('testlink').close()
  ev1, ev2 = w.read(0)
  assert ev1.open and ev2.close
  w.remove_path('testfile')
  open('testlink').close()
  ev = w.read(0)
  assert any(e.close for e in ev)


def test_delete(w):
    w.add('testfile', inotify.IN_DELETE_SELF)
    os.remove('testfile')
    ev1, ev2 = w.read(0)
    assert ev1.delete_self
    assert ev2.ignored
    assert w.num_watches() == 0

def test_noent(w):
    with pytest.raises(OSError) as excinfo:
        w.add('nonexistant', inotify.IN_OPEN)
    assert excinfo.value.errno == os.errno.ENOENT
    with pytest.raises(OSError) as excinfo:
        w.add_all('nonexistant', inotify.IN_OPEN)
    assert excinfo.value.errno == os.errno.ENOENT

def test_removewatch(w):
  'test Watcher.remove_path and Watcher.remove_watch functionality'
  open('testfile2', 'w').close()
  open('testfile3', 'w').close()
  watch1 = w.add('testfile', inotify.IN_OPEN)
  watch2 = w.add('testfile2', inotify.IN_OPEN)
  watch3 = w.add('testfile3', inotify.IN_OPEN)
  open('testfile').close()
  open('testfile2').close()
  open('testfile3').close()
  evts = w.read()
  assert [e.fullpath for e in evts] == ['testfile', 'testfile2', 'testfile3']
  assert all(e.mask & inotify.IN_OPEN for e in evts)

  w.remove_path('testfile')
  w.read()
  open('testfile').close()
  open('testfile2').close()
  open('testfile3').close()
  evts = w.read()
  assert [e.fullpath for e in evts] == ['testfile2', 'testfile3']
  assert all(e.mask & inotify.IN_OPEN for e in evts)

  w.remove_watch(watch2)
  w.read()
  open('testfile').close()
  open('testfile2').close()
  open('testfile3').close()
  evts = w.read()
  assert [e.fullpath for e in evts] == ['testfile3']
  assert all(e.mask & inotify.IN_OPEN for e in evts)

