#!/bin/python

from ctypes import *
import asyncio
import os
from itertools import count, takewhile
import struct
from types import MethodType
import concurrent
import threading
import time
from fibre.utils import Logger, Event
import platform

lib_names = {
    ('Linux', 'x86_64'): 'libfibre-linux-amd64.so',
    ('Linux', 'armv7l'): 'libfibre-linux-armhf.so',
    ('Windows', 'AMD64'): 'libfibre-windows-amd64.dll',
    ('Darwin', 'x86_64'): 'libfibre-macos-x86.dylib'
}

system_desc = (platform.system(), platform.machine())

lib_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    'cpp')

def test_path(path):
    return path if os.path.isfile(path) else None

lib_path = (test_path(os.path.join(lib_dir, 'libfibre.so')) or
            test_path(os.path.join(lib_dir, 'libfibre.dll')) or
            (test_path(os.path.join(lib_dir, lib_names[system_desc])) if (system_desc in lib_names) else None))

if lib_path is None:
    raise ModuleNotFoundError("This package has no precompiled libfibre for your platform ({} {}). "
        "Go to fibre/cpp/ and run `make` to compile libfibre for your platform.".format(*system_desc))

lib = windll.LoadLibrary(lib_path) if os.name == 'nt' else cdll.LoadLibrary(lib_path)


# libfibre definitions --------------------------------------------------------#

PostSignature = CFUNCTYPE(c_void_p, CFUNCTYPE(None, c_void_p), POINTER(c_int))
RegisterEventSignature = CFUNCTYPE(c_int, c_int, c_uint32, CFUNCTYPE(None, c_void_p), POINTER(c_int))
DeregisterEventSignature = CFUNCTYPE(c_int, c_int)
CallLaterSignature = CFUNCTYPE(c_void_p, c_float, CFUNCTYPE(None, c_void_p), POINTER(c_int))
CancelTimerSignature = CFUNCTYPE(c_int, c_void_p)
ConstructObjectSignature = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p, c_void_p, c_size_t)
DestroyObjectSignature = CFUNCTYPE(None, c_void_p, c_void_p)

OnFoundObjectSignature = CFUNCTYPE(None, c_void_p, c_void_p)
OnStoppedSignature = CFUNCTYPE(None, c_void_p, c_int)

OnAttributeAddedSignature = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p, c_size_t, c_void_p, c_void_p, c_size_t)
OnAttributeRemovedSignature = CFUNCTYPE(None, c_void_p, c_void_p)
OnFunctionAddedSignature = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p, c_size_t, POINTER(c_char_p), POINTER(c_char_p), POINTER(c_char_p), POINTER(c_char_p))
OnFunctionRemovedSignature = CFUNCTYPE(None, c_void_p, c_void_p)

OnCallCompletedSignature = CFUNCTYPE(None, c_void_p, c_int, c_char_p)

kFibreOk = 0
kFibreCancelled = 1
kFibreClosed = 2
kFibreInvalidArgument = 3
kFibreInternalError = 4

class LibFibreVersion(Structure):
    _fields_ = [
        ("major", c_uint16),
        ("minor", c_uint16),
        ("patch", c_uint16),
    ]

    def __repr__(self):
        return "{}.{}.{}".format(self.major, self.minor, self.patch)

libfibre_get_version = lib.libfibre_get_version
libfibre_get_version.argtypes = []
libfibre_get_version.restype = POINTER(LibFibreVersion)

version = libfibre_get_version().contents
if version.major != 0:
    raise Exception("Incompatible libfibre version: {}".format(version))

libfibre_open = lib.libfibre_open
libfibre_open.argtypes = [PostSignature, RegisterEventSignature, DeregisterEventSignature, CallLaterSignature, CancelTimerSignature, ConstructObjectSignature, DestroyObjectSignature, c_void_p]
libfibre_open.restype = c_void_p

libfibre_close = lib.libfibre_close
libfibre_close.argtypes = [c_void_p]
libfibre_close.restype = None

libfibre_start_discovery = lib.libfibre_start_discovery
libfibre_start_discovery.argtypes = [c_void_p, c_char_p, c_size_t, c_void_p, OnFoundObjectSignature, OnStoppedSignature, c_void_p]
libfibre_start_discovery.restype = c_void_p

libfibre_stop_discovery = lib.libfibre_stop_discovery
libfibre_stop_discovery.argtypes = [c_void_p, c_void_p]
libfibre_stop_discovery.restype = None

libfibre_subscribe_to_interface = lib.libfibre_subscribe_to_interface
libfibre_subscribe_to_interface.argtypes = [c_void_p, OnAttributeAddedSignature, OnAttributeRemovedSignature, OnFunctionAddedSignature, OnFunctionRemovedSignature, c_void_p]
libfibre_subscribe_to_interface.restype = None

libfibre_get_attribute = lib.libfibre_get_attribute
libfibre_get_attribute.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
libfibre_get_attribute.restype = c_int

libfibre_start_call = lib.libfibre_start_call
libfibre_start_call.argtypes = [c_void_p, c_void_p, c_char_p, c_size_t, c_char_p, c_size_t, c_void_p, OnCallCompletedSignature, c_void_p]
libfibre_start_call.restype = None

libfibre_cancel_call = lib.libfibre_cancel_call
libfibre_cancel_call.argtypes = [c_void_p]
libfibre_cancel_call.restype = None


# libfibre wrapper ------------------------------------------------------------#

class ObjectLostError(Exception):
    def __init__(self):
        super(Exception, self).__init__("the object disappeared")

def _get_exception(status):
    if status == kFibreOk:
        return None
    elif status == kFibreCancelled:
        return asyncio.CancelledError()
    elif status == kFibreClosed:
        return ObjectLostError()
    elif status == kFibreInvalidArgument:
        return ArgumentError()
    elif status == kFibreInternalError:
        return Exception("internal libfibre error")
    else:
        return Exception("unknown libfibre error {}".format(status))

class StructCodec():
    """
    Generic serializer/deserializer based on struct pack
    """
    def __init__(self, struct_format, target_type):
        self._struct_format = struct_format
        self._target_type = target_type
    def get_length(self):
        return struct.calcsize(self._struct_format)
    def serialize(self, libfibre, value):
        value = self._target_type(value)
        return struct.pack(self._struct_format, value)
    def deserialize(self, libfibre, buffer):
        value = struct.unpack(self._struct_format, buffer)
        value = value[0] if len(value) == 1 else value
        return self._target_type(value)

class ObjectPtrCodec():
    """
    Serializer/deserializer for an object reference

    libfibre transcodes object references internally from/to something that can
    be sent over the wire and understood by the remote instance.
    """
    def get_length(self):
        return struct.calcsize("P")
    def serialize(self, libfibre, value):
        if value is None:
            return struct.pack("P", 0)
        elif isinstance(value, RemoteObject):
            return struct.pack("P", value._obj_handle)
        else:
            raise TypeError("Expected value of type RemoteObject or None but got '{}'. An example for a RemoteObject is this expression: odrv0.axis0.controller._input_pos_property".format(type(value).__name__))
    def deserialize(self, libfibre, buffer):
        handle = struct.unpack("P", buffer)[0]
        return None if handle == 0 else libfibre._objects[handle]


codecs = {
    'int8': StructCodec("<b", int),
    'uint8': StructCodec("<B", int),
    'int16': StructCodec("<h", int),
    'uint16': StructCodec("<H", int),
    'int32': StructCodec("<i", int),
    'uint32': StructCodec("<I", int),
    'int64': StructCodec("<q", int),
    'uint64': StructCodec("<Q", int),
    'bool': StructCodec("<?", bool),
    'float': StructCodec("<f", float),
    'object_ref': ObjectPtrCodec()
}

def decode_arg_list(arg_names, codec_names):
    for i in count(0):
        if arg_names[i] is None or codec_names[i] is None:
            break
        arg_name = arg_names[i].decode('utf-8')
        codec_name = codec_names[i].decode('utf-8')
        if not codec_name in codecs:
            raise Exception("unsupported codec {}".format(codec_name))
        yield arg_name, codec_name, codecs[codec_name]

def insert_with_new_id(dictionary, val):
    key = next(x for x in count(1) if x not in set(dictionary.keys()))
    dictionary[key] = val
    return key

# Runs a function on a foreign event loop and blocks until the function is done.
def run_coroutine_threadsafe(loop, func):
    future = concurrent.futures.Future()
    async def func_async():
        try:
            future.set_result(await func())
        except Exception as ex:
            future.set_exception(ex)
    loop.call_soon_threadsafe(asyncio.ensure_future, func_async())
    return future.result()

class RemoteFunction(object):
    """
    Represents a callable function that maps to a function call on a remote object.
    """
    def __init__(self, libfibre, func_handle, inputs, outputs):
        self._libfibre = libfibre
        self._func_handle = func_handle
        self._inputs = inputs
        self._outputs = outputs
        self._rx_size = sum(codec.get_length() for _, _, codec in self._outputs)
        self._calls = {}
        self._c_on_completed = OnCallCompletedSignature(self._on_completed)

    def _on_completed(self, ctx, status, end_ptr):
        call = self._calls.pop(ctx)

        if status != kFibreOk:
            call['future'].set_exception(_get_exception(status))
        else:
            pos = 0
            outputs = []

            for arg in self._outputs:
                arg_length = arg[2].get_length()
                outputs.append(arg[2].deserialize(self._libfibre, call['rx_buf'][pos:(pos + arg_length)]))
                pos += arg_length

            if len(outputs) == 0:
                call['future'].set_result(None)
            elif len(outputs) == 1:
                call['future'].set_result(outputs[0])
            else:
                call['future'].set_result(tuple(outputs))

    def __call__(self, instance, *args):
        """
        Starts invoking the function on the remote object.
        If this function is called from the Fibre thread then it is nonblocking
        and returns an asyncio.Future. If it is called from another thread then
        it blocks until the function completes and returns the result(s) of the 
        invokation.
        """

        if threading.current_thread() != libfibre_thread:
            return run_coroutine_threadsafe(instance._libfibre.loop, lambda: self.__call__(instance, *args))

        if (len(self._inputs) != len(args)):
            raise TypeError("expected {} arguments but have {}".format(len(self._inputs), len(args)))

        # All of these variables need to be protected from the garbage collector
        # for the duration of the call.
        call = {
            'handle': c_size_t(0),
            'tx_buf': b''.join(self._inputs[i][2].serialize(self._libfibre, args[i])
                               for i in range(len(self._inputs))), # Assemble TX buffer
            'rx_buf': b'\0' * self._rx_size, # Allocate RX buffer
            'future': instance._libfibre.loop.create_future(),
        }
        call_id = insert_with_new_id(self._calls, call)

        libfibre_start_call(instance._obj_handle, self._func_handle,
            cast(call['tx_buf'], c_char_p), len(call['tx_buf']),
            cast(call['rx_buf'], c_char_p), len(call['rx_buf']),
            byref(call['handle']), self._c_on_completed, call_id)
        
        return call['future']

    def __get__(self, instance, owner):
        return MethodType(self, instance) if instance else self

    def _dump(self, name):
        print_arglist = lambda arglist: ", ".join("{}: {}".format(arg_name, codec_name) for arg_name, codec_name, codec in arglist)
        return "{}({}){}".format(name,
            print_arglist(self._inputs),
            "" if len(self._outputs) == 0 else
            " -> " + print_arglist(self._outputs) if len(self._outputs) == 1 else
            " -> (" + print_arglist(self._outputs) + ")")

class RemoteAttribute(object):
    def __init__(self, libfibre, attr_handle, intf_handle, intf_name, magic_getter, magic_setter):
        self._libfibre = libfibre
        self._attr_handle = attr_handle
        self._intf_handle = intf_handle
        self._intf_name = intf_name
        self._magic_getter = magic_getter
        self._magic_setter = magic_setter

    def _get_obj(self, instance):
        py_intf = self._libfibre._load_py_intf(self._intf_name, self._intf_handle)

        obj_handle = c_void_p(0)
        status = libfibre_get_attribute(instance._obj_handle, self._attr_handle, byref(obj_handle))
        if status != kFibreOk:
            raise _get_exception(status)
        
        return self._libfibre._objects[obj_handle.value]

    def __get__(self, instance, owner):
        if not instance:
            return self

        if self._magic_getter:
            return self._get_obj(instance).read()
        else:
            return self._get_obj(instance)

    def __set__(self, instance, val):
        if self._magic_setter:
            self._get_obj(instance).exchange(val)
        else:
            raise Exception("this attribute cannot be written to")


class RemoteObject(object):
    """
    Base class for interfaces of remote objects.
    """
    __sealed__ = False

    def __init__(self, libfibre, obj_handle):
        self.__class__._refcount += 1

        self._libfibre = libfibre
        self._obj_handle = obj_handle
        self._on_lost = concurrent.futures.Future() # TODO: maybe we can do this with conc

        # Ensure that assignments to undefined attributes raise an exception
        self.__sealed__ = True

    def __setattr__(self, key, value):
        if self.__sealed__ and not hasattr(self, key):
            raise AttributeError("Attribute {} not found".format(key))
        object.__setattr__(self, key, value)

    #def __del__(self):
    #    print("unref")
    #    libfibre_unref_obj(self._obj_handle)

    def _dump(self, indent, depth):
        if self._obj_handle is None:
            return "[object lost]"

        try:
            if depth <= 0:
                return "..."
            lines = []
            for key in dir(self.__class__):
                if key.startswith('_'):
                    continue
                class_member = getattr(self.__class__, key)
                if isinstance(class_member, RemoteFunction):
                    lines.append(indent + class_member._dump(key))
                elif isinstance(class_member, RemoteAttribute):
                    val = getattr(self, key)
                    if isinstance(val, RemoteObject) and not class_member._magic_getter:
                        lines.append(indent + key + (": " if depth == 1 else ":\n") + val._dump(indent + "  ", depth - 1))
                    else:
                        if isinstance(val, RemoteObject) and class_member._magic_getter:
                            val_str = get_user_name(val)
                        else:
                            val_str = str(val)
                        property_type = str(class_member._get_obj(self).__class__.read._outputs[0][1])
                        lines.append(indent + key + ": " + val_str + " (" + property_type + ")")
                else:
                    lines.append(indent + key + ": " + str(type(val)))
        except:
            return "[failed to dump object]"

        return "\n".join(lines)

    def __str__(self):
        return self._dump("", depth=2)

    def __repr__(self):
        return self.__str__()

    def _destroy(self):
        libfibre = self._libfibre
        on_lost = self._on_lost

        self._libfibre = None
        self._obj_handle = None
        self._on_lost = None

        self.__class__._refcount -= 1
        if self.__class__._refcount == 0:
            libfibre.interfaces.pop(self.__class__._handle)

        on_lost.set_result(True)


class LibFibre():
    def __init__(self):
        self.loop = asyncio.get_event_loop()

        # We must keep a reference to these function objects so they don't get
        # garbage collected.
        self.c_post = PostSignature(self._post)
        self.c_register_event = RegisterEventSignature(self._register_event)
        self.c_deregister_event = DeregisterEventSignature(self._deregister_event)
        self.c_call_later = CallLaterSignature(self._call_later)
        self.c_cancel_timer = CancelTimerSignature(self._cancel_timer)
        self.c_construct_object = ConstructObjectSignature(self._construct_object)
        self.c_destroy_object = DestroyObjectSignature(self._destroy_object)
        self.c_on_found_object = OnFoundObjectSignature(self._on_found_object)
        self.c_on_discovery_stopped = OnStoppedSignature(self._on_discovery_stopped)
        self.c_on_attribute_added = OnAttributeAddedSignature(self._on_attribute_added)
        self.c_on_attribute_removed = OnAttributeRemovedSignature(self._on_attribute_removed)
        self.c_on_function_added = OnFunctionAddedSignature(self._on_function_added)
        self.c_on_function_removed = OnFunctionRemovedSignature(self._on_function_removed)
        
        self.timer_map = {}
        self.eventfd_map = {}
        self.interfaces = {} # key: libfibre handle, value: python class
        self.discovery_processes = {} # key: ID, value: python dict
        self._objects = {} # key: libfibre handle, value: pyhton class

        self.ctx = c_void_p(libfibre_open(
            self.c_post,
            self.c_register_event, self.c_deregister_event,
            self.c_call_later, self.c_cancel_timer,
            self.c_construct_object, self.c_destroy_object, None))

    def _post(self, callback, ctx):
        self.loop.call_soon_threadsafe(callback, ctx)

    def _register_event(self, event_fd, events, callback, ctx):
        self.eventfd_map[event_fd] = events
        if (events & 1):
            self.loop.add_reader(event_fd, callback, ctx)
        if (events & 4):
            self.loop.add_writer(event_fd, callback, ctx)
        if (events & 0xfffffffa):
            raise Exception("unsupported event mask " + str(events))
        return 0

    def _deregister_event(self, event_fd):
        events = self.eventfd_map.pop(event_fd)
        if (events & 1):
            self.loop.remove_reader(event_fd)
        if (events & 4):
            self.loop.remove_writer(event_fd)
        return 0

    def _call_later(self, delay, callback, ctx):
        timer_id = insert_with_new_id(self.timer_map, self.loop.call_later(delay, callback, ctx))
        return timer_id

    def _cancel_timer(self, timer_id):
        self.timer_map.pop(timer_id).cancel()
        return 0

    def _load_py_intf(self, name, intf_handle):
        """
        Creates a new python type for the specified libfibre interface handle or
        returns the existing python type if one was already create before.

        Behind the scenes the python type will react to future events coming
        from libfibre, such as functions/attributes being added/removed.
        """
        if intf_handle in self.interfaces:
            return self.interfaces[intf_handle]
        else:
            if name is None:
                name = "anonymous_interface_" + str(intf_handle)
            py_intf = self.interfaces[intf_handle] = type(name, (RemoteObject,), {'_handle': intf_handle, '_refcount': 0})
            #exit(1)
            libfibre_subscribe_to_interface(intf_handle, self.c_on_attribute_added, self.c_on_attribute_removed, self.c_on_function_added, self.c_on_function_removed, intf_handle)
            return py_intf

    def _construct_object(self, ctx, obj, intf, name, name_length):
        #increment_libfibre_refcount()
        name = None if name is None else string_at(name, name_length).decode('utf-8')
        py_intf = self._load_py_intf(name, intf)
        assert(not obj in self._objects)
        self._objects[obj] = py_intf(self, obj)

    def _destroy_object(self, ctx, obj):
        py_obj = self._objects.pop(obj)
        py_obj._destroy()
        #decrement_lib_refcount()

    def _on_found_object(self, ctx, obj):
        py_obj = self._objects[obj]
        # notify the subscriber
        asyncio.ensure_future(self.discovery_processes[ctx]['callback'](py_obj))
    
    def _on_discovery_stopped(self, ctx, result):
        print("discovery stopped")

    def _on_attribute_added(self, ctx, attr, name, name_length, subintf, subintf_name, subintf_name_length):
        name = string_at(name, name_length).decode('utf-8')
        subintf_name = None if subintf_name is None else string_at(subintf_name, subintf_name_length).decode('utf-8')
        intf = self.interfaces[ctx]

        magic_getter = not subintf_name is None and subintf_name.startswith("fibre.Property<") and subintf_name.endswith(">")
        magic_setter = not subintf_name is None and subintf_name.startswith("fibre.Property<readwrite ") and subintf_name.endswith(">")

        setattr(intf, name, RemoteAttribute(self, attr, subintf, subintf_name, magic_getter, magic_setter))
        if magic_getter or magic_setter:
            setattr(intf, "_" + name + "_property", RemoteAttribute(self, attr, subintf, subintf_name, False, False))

    def _on_attribute_removed(self, ctx, attr):
        print("attribute removed")

    def _on_function_added(self, ctx, func, name, name_length, input_names, input_codecs, output_names, output_codecs):
        name = string_at(name, name_length).decode('utf-8')
        inputs = list(decode_arg_list(input_names, input_codecs))
        outputs = list(decode_arg_list(output_names, output_codecs))
        intf = self.interfaces[ctx]
        setattr(intf, name, RemoteFunction(self, func, inputs, outputs))

    def _on_function_removed(self, ctx, func):
        print("function removed")

    def start_discovery(self, path, on_obj_discovered, cancellation_token):
        buf = path.encode('ascii')

        discovery = {
            'handle': c_void_p(0),
            'callback': on_obj_discovered
        }
        discovery_id = insert_with_new_id(self.discovery_processes, discovery)

        cancellation_token.subscribe(lambda: libfibre_stop_discovery(self.ctx, discovery['handle']))
        libfibre_start_discovery(self.ctx, buf, len(buf), byref(discovery['handle']), self.c_on_found_object, self.c_on_discovery_stopped, discovery_id)



libfibre = None

def run_event_loop():
    global libfibre
    global terminate_libfibre

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    terminate_libfibre = loop.create_future()
    libfibre = LibFibre()

    libfibre.loop.run_until_complete(terminate_libfibre)

    libfibre_close(libfibre.ctx)

    # Detach all objects that still exist
    # TODO: the proper way would be either of these
    #  - provide a libfibre function to destroy an object on-demand which we'd
    #    call before libfibre_close().
    #  - have libfibre_close() report the destruction of all objects

    while len(libfibre._objects):
        libfibre._objects.pop(list(libfibre._objects.keys())[0])._destroy()
    assert(len(libfibre.interfaces) == 0)

    libfibre = None


lock = threading.Lock()
libfibre_refcount = 0
libfibre_thread = None

def increment_libfibre_refcount():
    global libfibre_refcount
    global libfibre_thread

    with lock:
        libfibre_refcount += 1
        #print("inc refcount to {}".format(libfibre_refcount))

        if libfibre_refcount == 1:
            libfibre_thread = threading.Thread(target = run_event_loop)
            libfibre_thread.start()

        while libfibre is None:
            time.sleep(0.1)

def decrement_lib_refcount():
    global libfibre_refcount
    global libfibre_thread

    with lock:
        #print("dec refcount from {}".format(libfibre_refcount))
        libfibre_refcount -= 1

        if libfibre_refcount == 0:
            libfibre.loop.call_soon_threadsafe(lambda: terminate_libfibre.set_result(True))

            # It's unlikely that releasing fibre from a fibre callback is ok. If
            # there is a valid scenario for this then we can remove the assert.
            assert(libfibre_thread != threading.current_thread())

            libfibre_thread.join()
            libfibre_thread = None


def find_all(path, serial_number,
         on_object_discovered,
         search_cancellation_token,
         channel_termination_token,
         logger):
    """
    Starts scanning for Fibre objects that match the specified path spec and calls
    the callback for each Fibre object that is found.

    This function is non-blocking and thread-safe.
    """

    async def on_object_discovered_filter(obj):
        increment_libfibre_refcount()
        channel_termination_token.subscribe(lambda: decrement_lib_refcount())
        if serial_number is None or (await fibre.utils.get_serial_number_str(obj)) == serial_number:
            result = on_object_discovered(obj)
            if not result is None:
                await result
    
    increment_libfibre_refcount()
    search_cancellation_token.subscribe(lambda: decrement_lib_refcount())

    libfibre.loop.call_soon_threadsafe(lambda: libfibre.start_discovery(
        path,
        on_object_discovered_filter,
        search_cancellation_token))

def find_any(path="usb", serial_number=None,
        search_cancellation_token=None, channel_termination_token=None,
        timeout=None, logger=Logger(verbose=False), find_multiple=False):
    """
    Blocks until the first matching Fibre object is connected and then returns that object
    """
    result = []
    done_signal = Event(search_cancellation_token)
    def did_discover_object(obj):
        result.append(obj)
        if find_multiple:
            if len(result) >= int(find_multiple):
               done_signal.set()
        else:
            done_signal.set()

    find_all(path, serial_number, did_discover_object, done_signal, channel_termination_token, logger)

    try:
        done_signal.wait(timeout=timeout)
    except TimeoutError:
        if not find_multiple:
            return None
    finally:
        done_signal.set() # terminate find_all

    if find_multiple:
        return result
    else:
        return result[0] if len(result) > 0 else None


def get_user_name(obj):
    """
    Can be overridden by the application to return the user-facing name of an
    object.
    """
    return "[anonymous object]"
