"""
This module queries process memory allocation metrics from the operating
system. It provides a platform independent layer to get the amount of virtual
and physical memory allocated to the Python process.

Different mechanisms are implemented: Either the process stat file is read
(Linux), the `ps` command is executed (BSD/OSX/Solaris) or the resource module
is queried (Unix fallback). On Windows try to use the win32 module if
available. If all fails, return 0 for each attribute.

Windows without the win32 module is not supported.

    >>> from pympler.process import ProcessMemoryInfo
    >>> pmi = ProcessMemoryInfo()
    >>> print "Virtual size [Byte]:", pmi.vsz # doctest: +ELLIPSIS
    Virtual size [Byte]: ...
"""

import threading

try:
    import thread
except ImportError:
    import _thread as thread

from os import getpid
from subprocess  import Popen, PIPE

try:
    from resource import getpagesize as _getpagesize
except ImportError:
    _getpagesize = lambda : 4096

class _ProcessMemoryInfo(object):
    """Stores information about various process-level memory metrics. The
    virtual size is stored in attribute `vsz`, the physical memory allocated to
    the process in `rss`, and the number of (major) pagefaults in `pagefaults`.
    On Linux, `data_segment`, `code_segment`, `shared_segment` and
    `stack_segment` contain the number of Bytes allocated for the respective
    segments.  This is an abstract base class which needs to be overridden by
    operating system specific implementations. This is done when importing the
    module.
    """

    pagesize = _getpagesize()

    def __init__(self):
        self.pid = getpid()
        self.rss = 0
        self.vsz = 0
        self.pagefaults = 0
        self.os_specific = []

        self.data_segment = 0
        self.code_segment = 0
        self.shared_segment = 0
        self.stack_segment = 0

        self.available = self.update()

    def update(self):
        """
        Refresh the information using platform instruments. Returns true if this
        operation yields useful values on the current platform.
        """
        return False

ProcessMemoryInfo = _ProcessMemoryInfo

def is_available():
    """
    Convenience function to check if the current platform is supported by this
    module.
    """
    return ProcessMemoryInfo().update()

class _ProcessMemoryInfoPS(_ProcessMemoryInfo):
    def update(self):
        """
        Get virtual and resident size of current process via 'ps'.
        This should work for MacOS X, Solaris, Linux. Returns true if it was
        successful.
        """
        try:
            p = Popen(['/bin/ps', '-p%s' % self.pid, '-o', 'rss,vsz'],
                      stdout=PIPE, stderr=PIPE)
        except OSError:
            pass
        else:
            s = p.communicate()[0].split()
            if p.returncode == 0 and len(s) >= 2:
                self.vsz = int(s[-1]) * 1024
                self.rss = int(s[-2]) * 1024
                return True
        return False


class _ProcessMemoryInfoProc(_ProcessMemoryInfo):

    key_map = {
        'VmPeak'       : 'Peak virtual memory size',
        'VmSize'       : 'Virtual memory size',
        'VmLck'        : 'Locked memory size',
        'VmHWM'        : 'Peak resident set size',
        'VmRSS'        : 'Resident set size',
        'VmStk'        : 'Size of stack segment',
        'VmData'       : 'Size of data segment',
        'VmExe'        : 'Size of code segment',
        'VmLib'        : 'Shared library code size',
        'VmPTE'        : 'Page table entries size',
    }

    def update(self):
        """
        Get virtual size of current process by reading the process' stat file.
        This should work for Linux.
        """
        try:
            stat = open('/proc/self/stat')
            status = open('/proc/self/status')
        except IOError:
            pass
        else:
            stats = stat.read().split()
            self.vsz = int( stats[22] )
            self.rss = int( stats[23] ) * self.pagesize
            self.pagefaults = int( stats[11] )

            for entry in status.readlines():
                key, value = entry.split(':')
                size_in_bytes = lambda x: int(x.split()[0]) * 1024
                if key == 'VmData':
                    self.data_segment = size_in_bytes(value)
                elif key == 'VmExe':
                    self.code_segment = size_in_bytes(value)
                elif key == 'VmLib':
                    self.shared_segment = size_in_bytes(value)
                elif key == 'VmStk':
                    self.stack_segment = size_in_bytes(value)
                key = self.key_map.get(key)
                if key:
                    self.os_specific.append((key, value.strip()))


            stat.close()
            status.close()
            return True
        return False


try:
    from resource import getrusage, RUSAGE_SELF

    class _ProcessMemoryInfoResource(_ProcessMemoryInfo):
        def update(self):
            """
            Get memory metrics of current process through `getrusage`.  Only
            available on Unix, on Linux most of the fields are not set,
            and on BSD units are used that are not very helpful, see:

            http://www.perlmonks.org/?node_id=626693

            Furthermore, getrusage only provides accumulated statistics (e.g.
            max rss vs current rss).
            """
            usage = getrusage(RUSAGE_SELF)
            self.rss = usage.ru_maxrss * 1024
            self.data_segment = usage.ru_idrss * 1024 # TODO: ticks?
            self.shared_segment = usage.ru_ixrss * 1024 # TODO: ticks?
            self.stack_segment = usage.ru_isrss * 1024 # TODO: ticks?
            self.vsz = self.data_segment + self.shared_segment + \
                       self.stack_segment

            self.pagefaults = usage.ru_majflt
            return self.rss != 0

    if _ProcessMemoryInfoProc().update():
        ProcessMemoryInfo = _ProcessMemoryInfoProc
    elif _ProcessMemoryInfoPS().update():
        ProcessMemoryInfo = _ProcessMemoryInfoPS
    elif _ProcessMemoryInfoResource().update():
        ProcessMemoryInfo = _ProcessMemoryInfoResource

except ImportError:
    try:
        # Requires pywin32
        from win32process import GetProcessMemoryInfo
        from win32api import GetCurrentProcess
    except ImportError:
        # TODO Emit Warning:
        #print "It is recommended to install pywin32 when running pympler on Microsoft Windows."
        pass
    else:
        class _ProcessMemoryInfoWin32(_ProcessMemoryInfo):
            def update(self):
                process_handle = GetCurrentProcess()
                memory_info = GetProcessMemoryInfo( process_handle )
                self.vsz        = memory_info['PagefileUsage']
                self.rss        = memory_info['WorkingSetSize']
                self.pagefaults = memory_info['PageFaultCount']
                return True

        ProcessMemoryInfo = _ProcessMemoryInfoWin32


class ThreadInfo(object):
    """Collect information about an active thread."""

    def __init__(self, thread):
        self.ident = 0
        try:
            self.ident = thread.ident
        except AttributeError:
            # Thread.ident was introduced in Python 2.6. On Python 2.5 use the
            # undocumented `_active` dictionary to map thread objects to thread
            # IDs.
            for tid, athread in threading._active.items():
                if athread is thread:
                    self.ident = tid
                    break
        try:
            self.name = thread.name
        except AttributeError:
            self.name = thread.getName()
        try:
            self.daemon = thread.daemon
        except AttributeError:
            self.daemon = thread.isDaemon()


def get_current_threads():
    """Get a list of `ThreadInfo` objects."""
    return [ThreadInfo(thread) for thread in threading.enumerate()]


def get_current_thread_id():
    """Get the ID of the current thread."""
    return thread.get_ident()

