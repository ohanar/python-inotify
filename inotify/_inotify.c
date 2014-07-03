/*
 * _inotify.c - Python extension interfacing to the Linux inotify subsystem
 *
 * Copyright 2006 Bryan O'Sullivan <bos@serpentine.com>
 * Copyright 2012-2013 Jan Kanis <jan.code@jankanis.nl>
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of version 2.1 of the GNU Lesser General
 * Public License, incorporated herein by reference.
 * 
 * Additionally, code written by Jan Kanis may also be redistributed and/or 
 * modified under the terms of any version of the GNU Lesser General Public 
 * License greater than 2.1. 
 */

#include <Python.h>
#include <alloca.h>
#include <sys/inotify.h>
#include <stdint.h>
#include <sys/ioctl.h>
#include <unistd.h>

/* This should be at least large enough to hold a single
 * inotify_event, otherwise crashes can occur. */
#define READ_BUF_SIZE 64*1024

/* for older pythons */
#ifndef Py_TYPE
	#define Py_TYPE(ob) (((PyObject*)(ob))->ob_type)
#endif

/* for Python 2.5 and below */
#ifndef PyVarObject_HEAD_INIT
	#define PyVarObject_HEAD_INIT(type, size) \
		PyObject_HEAD_INIT(type) size,
#endif

#define min(a,b) \
 ({ __typeof__ (a) _a = (a); \
		 __typeof__ (b) _b = (b); \
	 _a < _b ? _a : _b; })

#define INE_SIZE sizeof(struct inotify_event)


static PyObject *init(PyObject *self, PyObject *args)
{
	PyObject *ret = NULL;
	int fd = -1;

	 if (!PyArg_ParseTuple(args, ":init"))
		goto bail;

	Py_BEGIN_ALLOW_THREADS
	fd = inotify_init();
	Py_END_ALLOW_THREADS

	if (fd == -1) {
		PyErr_SetFromErrno(PyExc_OSError);
		goto bail;
	}

	ret = PyLong_FromLong(fd);
	if (ret == NULL)
		goto bail;

	goto done;

bail:
	if (fd != -1)
		close(fd);

	Py_CLEAR(ret);

done:
	return ret;
}

PyDoc_STRVAR(
	init_doc,
	"init() -> fd\n"
	"\n"
	"Initialise an inotify instance.\n"
	"Return a file descriptor associated with a new inotify event queue.");

static PyObject *add_watch(PyObject *self, PyObject *args)
{
	PyObject *ret = NULL;
	uint32_t mask;
	int wd = -1;
	char *path;
	int fd;

	if (!PyArg_ParseTuple(args, "isI:add_watch", &fd, &path, &mask))
		goto bail;

	Py_BEGIN_ALLOW_THREADS
	wd = inotify_add_watch(fd, path, mask);
	Py_END_ALLOW_THREADS

	if (wd == -1) {
		PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
		goto bail;
	}

	ret = PyLong_FromLong(wd);
	if (ret == NULL)
		goto bail;

	goto done;

bail:
	if (wd != -1)
		inotify_rm_watch(fd, wd);

	Py_CLEAR(ret);

done:
	return ret;
}

PyDoc_STRVAR(
	add_watch_doc,
	"add_watch(fd, path, mask) -> wd\n"
	"\n"
	"Add a watch to an inotify instance, or modify an existing watch.\n"
	"\n"
	"        fd: file descriptor returned by init()\n"
	"        path: path to watch\n"
	"        mask: mask of events to watch for\n"
	"\n"
	"Return a unique numeric watch descriptor for the inotify instance\n"
	"mapped by the file descriptor.");

static PyObject *remove_watch(PyObject *self, PyObject *args)
{
	uint32_t wd;
	int fd;
	int r;

	if (!PyArg_ParseTuple(args, "iI:remove_watch", &fd, &wd))
		goto bail;

	Py_BEGIN_ALLOW_THREADS
	r = inotify_rm_watch(fd, wd);
	Py_END_ALLOW_THREADS

	if (r == -1) {
		PyErr_SetFromErrno(PyExc_OSError);
		goto bail;
	}

	Py_RETURN_NONE;

bail:
	return NULL;
}

PyDoc_STRVAR(
	remove_watch_doc,
	"remove_watch(fd, wd)\n"
	"\n"
	"        fd: file descriptor returned by init()\n"
	"        wd: watch descriptor returned by add_watch()\n"
	"\n"
	"Remove a watch associated with the watch descriptor wd from the\n"
	"inotify instance associated with the file descriptor fd.\n"
	"\n"
	"Removing a watch causes an IN_IGNORED event to be generated for this\n"
	"watch descriptor.");

#define bit_name(x) {x, #x}

static struct {
	unsigned int bit;
	const char *name;
	PyObject *pyname;
} bit_names[] = {
	bit_name(IN_ACCESS),
	bit_name(IN_MODIFY),
	bit_name(IN_ATTRIB),
	bit_name(IN_CLOSE_WRITE),
	bit_name(IN_CLOSE_NOWRITE),
	bit_name(IN_OPEN),
	bit_name(IN_MOVED_FROM),
	bit_name(IN_MOVED_TO),
	bit_name(IN_CREATE),
	bit_name(IN_DELETE),
	bit_name(IN_DELETE_SELF),
	bit_name(IN_MOVE_SELF),
	bit_name(IN_UNMOUNT),
	bit_name(IN_Q_OVERFLOW),
	bit_name(IN_IGNORED),
	bit_name(IN_ONLYDIR),
	bit_name(IN_DONT_FOLLOW),
	bit_name(IN_MASK_ADD),
	bit_name(IN_ISDIR),
	bit_name(IN_ONESHOT),
	bit_name(IN_EXCL_UNLINK),
	{0}
};

static PyObject *decode_mask(int mask)
{
	PyObject *ret = PyList_New(0);
	int i;

	if (ret == NULL)
		goto bail;

	for (i = 0; bit_names[i].bit; i++) {
		if (mask & bit_names[i].bit) {
			if (bit_names[i].pyname == NULL) {
				bit_names[i].pyname = PyUnicode_FromString(bit_names[i].name);
				if (bit_names[i].pyname == NULL)
					goto bail;
			}
			Py_INCREF(bit_names[i].pyname);
			if (PyList_Append(ret, bit_names[i].pyname) == -1)
				goto bail;
		}
	}

	goto done;

bail:
	Py_CLEAR(ret);

done:
	return ret;
}

static PyObject *pydecode_mask(PyObject *self, PyObject *args)
{
	int mask;

	if (!PyArg_ParseTuple(args, "i:decode_mask", &mask))
		return NULL;

	return decode_mask(mask);
}

PyDoc_STRVAR(
	decode_mask_doc,
	"decode_mask(mask) -> list_of_strings\n"
	"\n"
	"Decode an inotify mask value into a list of strings that give the\n"
	"name of each bit set in the mask.");

static char doc[] = "Low-level inotify interface wrappers.";

static void define_const(PyObject *dict, const char *name, uint32_t val)
{
	PyObject *pyval = PyLong_FromUnsignedLong(val);
	PyObject *pyname = PyUnicode_FromString(name);

	if (!pyname || !pyval)
		goto bail;

	PyDict_SetItem(dict, pyname, pyval);

bail:
	Py_XDECREF(pyname);
	Py_XDECREF(pyval);
}

static void define_consts(PyObject *dict)
{
	define_const(dict, "IN_ACCESS", IN_ACCESS);
	define_const(dict, "IN_MODIFY", IN_MODIFY);
	define_const(dict, "IN_ATTRIB", IN_ATTRIB);
	define_const(dict, "IN_CLOSE_WRITE", IN_CLOSE_WRITE);
	define_const(dict, "IN_CLOSE_NOWRITE", IN_CLOSE_NOWRITE);
	define_const(dict, "IN_OPEN", IN_OPEN);
	define_const(dict, "IN_MOVED_FROM", IN_MOVED_FROM);
	define_const(dict, "IN_MOVED_TO", IN_MOVED_TO);

	define_const(dict, "IN_CLOSE", IN_CLOSE);
	define_const(dict, "IN_MOVE", IN_MOVE);

	define_const(dict, "IN_CREATE", IN_CREATE);
	define_const(dict, "IN_DELETE", IN_DELETE);
	define_const(dict, "IN_DELETE_SELF", IN_DELETE_SELF);
	define_const(dict, "IN_MOVE_SELF", IN_MOVE_SELF);
	define_const(dict, "IN_UNMOUNT", IN_UNMOUNT);
	define_const(dict, "IN_Q_OVERFLOW", IN_Q_OVERFLOW);
	define_const(dict, "IN_IGNORED", IN_IGNORED);

	define_const(dict, "IN_ONLYDIR", IN_ONLYDIR);
	define_const(dict, "IN_DONT_FOLLOW", IN_DONT_FOLLOW);
	define_const(dict, "IN_MASK_ADD", IN_MASK_ADD);
	define_const(dict, "IN_ISDIR", IN_ISDIR);
	define_const(dict, "IN_EXCL_UNLINK", IN_EXCL_UNLINK);
	define_const(dict, "IN_ONESHOT", IN_ONESHOT);
	define_const(dict, "IN_ALL_EVENTS", IN_ALL_EVENTS);
}

// the event struct is not really doing anything that couldn't be done with a
// python named tuple, it should probably be replaced.
struct event {
	PyObject_HEAD
	PyObject *wd;
	PyObject *mask;
	PyObject *cookie;
	PyObject *name;
};

static PyObject *event_wd(PyObject *self, void *x)
{
	struct event *evt = (struct event *) self;
	Py_INCREF(evt->wd);
	return evt->wd;
}

static PyObject *event_mask(PyObject *self, void *x)
{
	struct event *evt = (struct event *) self;
	Py_INCREF(evt->mask);
	return evt->mask;
}

static PyObject *event_cookie(PyObject *self, void *x)
{
	struct event *evt = (struct event *) self;
	Py_INCREF(evt->cookie);
	return evt->cookie;
}

static PyObject *event_name(PyObject *self, void *x)
{
	struct event *evt = (struct event *) self;
	Py_INCREF(evt->name);
	return evt->name;
}

static struct PyGetSetDef event_getsets[] = {
	{"wd", event_wd, NULL,
	 "watch descriptor"},
	{"mask", event_mask, NULL,
	 "event mask"},
	{"cookie", event_cookie, NULL,
	 "rename cookie, if rename-related event"},
	{"name", event_name, NULL,
	 "file name"},
	{NULL}
};

PyDoc_STRVAR(
	event_doc,
	"event: Structure describing an inotify event.");

static PyObject *event_new(PyTypeObject *t, PyObject *a, PyObject *k)
{
	return (*t->tp_alloc)(t, 0);
}

static void event_dealloc(struct event *evt)
{
	Py_XDECREF(evt->wd);
	Py_XDECREF(evt->mask);
	Py_XDECREF(evt->cookie);
	Py_XDECREF(evt->name);

	(Py_TYPE(evt)->tp_free)(evt);
}

static PyObject *event_repr(struct event *evt)
{
	int wd = PyLong_AsLong(evt->wd);
	uint32_t cookie = evt->cookie == Py_None ? 0 : PyLong_AsLong(evt->cookie);
	PyObject *ret = NULL, *pymasks = NULL, *pymask = NULL;
	PyObject *join = NULL;

	join = PyUnicode_FromString("|");
	if (join == NULL)
		goto bail;

	pymasks = decode_mask(PyLong_AsLong(evt->mask));
	if (pymasks == NULL)
		goto bail;

	pymask = PyUnicode_Join(join, pymasks);
	if (pymask == NULL)
		goto bail;

	if (evt->name != Py_None) {
		PyObject *pyname = PyObject_Repr(evt->name);

#if PY_MAJOR_VERSION < 3
		if (pyname != NULL) {
			PyObject *unicode_pyname = PyObject_Unicode(pyname);
			Py_DECREF(pyname);
			pyname = unicode_pyname;
		} 
#endif

		if (cookie == 0)
			ret = PyUnicode_FromFormat("event(wd=%d, mask=%U, name=%V)",
										wd, pymask, pyname, "???");
		else
			ret = PyUnicode_FromFormat("event(wd=%d, mask=%U, "
										"cookie=0x%x, name=%V)",
									wd, pymask, cookie, pyname, "???");

		Py_XDECREF(pyname);
	} else {
		if (cookie == 0)
			ret = PyUnicode_FromFormat("event(wd=%d, mask=%U)",
									  wd, pymask);
		else {
			ret = PyUnicode_FromFormat("event(wd=%d, mask=%U, cookie=0x%x)",
									  wd, pymask, cookie);
		}
	}

	goto done;
bail:
	Py_CLEAR(ret);
	
done:
	Py_XDECREF(pymask);
	Py_XDECREF(pymasks);
	Py_XDECREF(join);

	return ret;
}

static PyTypeObject event_type = {
	PyVarObject_HEAD_INIT(NULL, 0)
	"_inotify.event",             /*tp_name*/
	sizeof(struct event), /*tp_basicsize*/
	0,                         /*tp_itemsize*/
	(destructor)event_dealloc, /*tp_dealloc*/
	0,                         /*tp_print*/
	0,                         /*tp_getattr*/
	0,                         /*tp_setattr*/
	0,                         /*tp_compare*/
	(reprfunc)event_repr,      /*tp_repr*/
	0,                         /*tp_as_number*/
	0,                         /*tp_as_sequence*/
	0,                         /*tp_as_mapping*/
	0,                         /*tp_hash */
	0,                         /*tp_call*/
	0,                         /*tp_str*/
	0,                         /*tp_getattro*/
	0,                         /*tp_setattro*/
	0,                         /*tp_as_buffer*/
	Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE, /*tp_flags*/
	event_doc,           /* tp_doc */
	0,                         /* tp_traverse */
	0,                         /* tp_clear */
	0,                         /* tp_richcompare */
	0,                         /* tp_weaklistoffset */
	0,                         /* tp_iter */
	0,                         /* tp_iternext */
	0,                         /* tp_methods */
	0,                         /* tp_members */
	event_getsets,      /* tp_getset */
	0,                         /* tp_base */
	0,                         /* tp_dict */
	0,                         /* tp_descr_get */
	0,                         /* tp_descr_set */
	0,                         /* tp_dictoffset */
	0,                         /* tp_init */
	0,                         /* tp_alloc */
	event_new,          /* tp_new */
};
	
static PyObject *read_events(PyObject *self, PyObject *args, PyObject *keywds)
{
	static char buffer[READ_BUF_SIZE];
	PyObject *ctor_args = NULL;
	PyObject *ret = NULL;
	int block = 1;
	int readable = 0;
	int pos, read_total, ioctl_retval;
	int fd;

	static char *kwlist[] = {"fd", "block", NULL};

#if PY_MAJOR_VERSION >= 3 && PY_MINOR_VERSION >= 3
	const char *format = "i|$p:read";
#else
	const char* format = "i|i:read";
	Py_ssize_t argc = PyTuple_Size(args);
	if (argc == -1)
		goto bail;
	if (argc > 1) {
		PyErr_Format(PyExc_TypeError, "read() takes exactly 1 positional argument but %zd were given", argc);
		goto bail;
	}
#endif

	if (!PyArg_ParseTupleAndKeywords(args, keywds, format, kwlist, &fd, &block))
		goto bail;

	ret = PyList_New(0);
	if (ret == NULL)
		goto bail;
	
	ctor_args = PyTuple_New(0);
	if (ctor_args == NULL)
		goto bail;

	Py_BEGIN_ALLOW_THREADS;
	ioctl_retval = ioctl(fd, FIONREAD, &readable);
	Py_END_ALLOW_THREADS;

	if (ioctl_retval < 0) {
		PyErr_SetFromErrno(PyExc_OSError);
		goto bail;
	}

	if (block == 0 && readable == 0) {
		goto done;
	}

	read_total = 0;
	pos = 0;

	do {
		int nread, size;
		int toread = min(readable - read_total, READ_BUF_SIZE - pos);

		Py_BEGIN_ALLOW_THREADS
		nread = read(fd, buffer + pos, toread);
		Py_END_ALLOW_THREADS;

		if (nread == -1) {
			PyErr_SetFromErrno(PyExc_OSError);
			goto bail;
		}

		read_total += nread;
		size = nread + pos;

		while (pos < size) {
			struct inotify_event *in = (struct inotify_event *) (buffer + pos);

			// order these comparisons so there won't be an overflow if in->len is very large
			if (size - pos < INE_SIZE || size - pos - INE_SIZE < in->len) {
				if (pos == 0 ||
						in->len > READ_BUF_SIZE - INE_SIZE ||
						in->len >= readable - (read_total - nread + pos + INE_SIZE)) {
					// This is not supposed to happen, unless we are reading
					// garbage. Maybe the fd wasn't an inotify fd?
					PyErr_Format(PyExc_TypeError, "python-inotify internal error: " 
							"read value from fd %i seems to be garbage, "
							"are you sure this is the right fd?", fd);
					goto bail;
				}
				// we read a partial message
				memcpy(buffer, buffer + pos, size - pos);
				pos = size - pos;
				goto nextread;
			}
			
			struct event *evt;
			PyObject *obj;

			obj = PyObject_CallObject((PyObject *) &event_type, ctor_args);

			if (obj == NULL)
				goto bail;

			evt = (struct event *) obj;

			evt->wd = PyLong_FromLong(in->wd);
			evt->mask = PyLong_FromLong(in->mask);
			if (in->mask & IN_MOVE)
				evt->cookie = PyLong_FromLong(in->cookie);
			else {
				Py_INCREF(Py_None);
				evt->cookie = Py_None;
			}
			if (in->len)
				evt->name = PyUnicode_FromString(in->name);
			else {
				Py_INCREF(Py_None);
				evt->name = Py_None;
			}

			if (!evt->wd || !evt->mask || !evt->cookie || !evt->name)
				goto mybail;

			if (PyList_Append(ret, obj) == -1)
				goto mybail;

			pos += sizeof(struct inotify_event) + in->len;
			Py_DECREF(obj);
			continue;

		mybail:
			Py_CLEAR(evt->wd);
			Py_CLEAR(evt->mask);
			Py_CLEAR(evt->cookie);
			Py_CLEAR(evt->name);
			Py_DECREF(obj);

			goto bail;
		}

		pos = 0;

	nextread:
		;

	} while (read_total < readable);
	
	goto done;

bail:
	Py_CLEAR(ret);
	
done:
	Py_XDECREF(ctor_args);

	return ret;
}

PyDoc_STRVAR(
	read_doc,
	"read(fd, *, block=True) -> list_of_events\n"
	"\n"
	"Read inotify events from a file descriptor.\n"
	"\n"
	"        fd: file descriptor returned by init()\n"
	"        block: If true, block if no events are available immediately.\n"
	"\n"
	"Return a list of event objects. read() will always return as many events as "
	"are available for reading at the moment the call to read() is made. \n"
	"\n");


static PyMethodDef methods[] = {
	{"init", init, METH_VARARGS, init_doc},
	{"add_watch", add_watch, METH_VARARGS, add_watch_doc},
	{"remove_watch", remove_watch, METH_VARARGS, remove_watch_doc},
	{"read", (PyCFunction) read_events, METH_VARARGS | METH_KEYWORDS, read_doc},
	{"decode_mask", pydecode_mask, METH_VARARGS, decode_mask_doc},
	{NULL},
};


#if PY_MAJOR_VERSION >= 3
PyMODINIT_FUNC PyInit__inotify(void)
{
	PyObject *mod, *dict;
	static struct PyModuleDef moduledef = {
		PyModuleDef_HEAD_INIT, "_inotify", doc, -1, methods, };

	if (PyType_Ready(&event_type) == -1)
		return NULL;

	mod = PyModule_Create(&moduledef);

	dict = PyModule_GetDict(mod);
	
	if (dict)
		define_consts(dict);

	return mod;
}

#else
void init_inotify(void)
{
	PyObject *mod, *dict;

	if (PyType_Ready(&event_type) == -1)
		return;

	mod = Py_InitModule3("_inotify", methods, doc);

	dict = PyModule_GetDict(mod);
	
	if (dict)
		define_consts(dict);

	return;
}
#endif
