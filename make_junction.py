"""
Creates a directory junction:
  datasets\TrafficLabelling  (trailing space) -> datasets\TrafficLabelling

Uses Windows raw API via ctypes so CMD/PowerShell cannot strip the trailing space.
"""
import ctypes
import ctypes.wintypes
import struct
import sys

kernel32 = ctypes.windll.kernel32

IO_REPARSE_TAG_MOUNT_POINT   = 0xA0000003
FSCTL_SET_REPARSE_POINT      = 0x000900A4
GENERIC_WRITE                = 0x40000000
OPEN_EXISTING                = 3
FILE_FLAG_BACKUP_SEMANTICS   = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE_VALUE         = ctypes.wintypes.HANDLE(-1).value

target_dir   = r'D:\Codingggg\Banglore hackathon\datasets\TrafficLabelling'
junction_raw = r'\\?\D:\Codingggg\Banglore hackathon\datasets\TrafficLabelling '  # trailing space

# 1. Remove if already exists (empty dir only)
kernel32.RemoveDirectoryW(junction_raw)

# 2. Create the directory with the trailing-space name
ok = kernel32.CreateDirectoryW(junction_raw, None)
print(f"CreateDirectory: {ok}  err={kernel32.GetLastError()}")

# 3. Open it for writing reparse data
handle = kernel32.CreateFileW(
    junction_raw,
    GENERIC_WRITE, 0, None, OPEN_EXISTING,
    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
    None
)
err = kernel32.GetLastError()
print(f"CreateFile handle: {handle}  err={err}")
if handle == INVALID_HANDLE_VALUE:
    sys.exit(f"ERROR: could not open directory (err={err})")

# 4. Build REPARSE_DATA_BUFFER for a mount-point (junction)
#    struct layout (little-endian):
#      ULONG  ReparseTag            (4 bytes)
#      USHORT ReparseDataLength     (2 bytes)
#      USHORT Reserved              (2 bytes)
#      -- MountPointReparseBuffer --
#      USHORT SubstituteNameOffset  (2 bytes)
#      USHORT SubstituteNameLength  (2 bytes)
#      USHORT PrintNameOffset       (2 bytes)
#      USHORT PrintNameLength       (2 bytes)
#      WCHAR  PathBuffer[1]         (variable)
#   format string: '<IHHHHHH'  (I + 6 shorts = 7 values)

sub_name  = ('\\??' + '\\' + target_dir).encode('utf-16-le')
prnt_name = target_dir.encode('utf-16-le')
path_buf  = sub_name + b'\x00\x00' + prnt_name + b'\x00\x00'

sub_off   = 0
sub_len   = len(sub_name)
prnt_off  = sub_len + 2          # after sub_name + null terminator
prnt_len  = len(prnt_name)
data_len  = 8 + len(path_buf)    # 8 = the 4 USHORT fields after Reserved

header = struct.pack(
    '<IHHHHHH',                  # 1xI + 6xH = 7 values
    IO_REPARSE_TAG_MOUNT_POINT,  # ReparseTag
    data_len,                    # ReparseDataLength
    0,                           # Reserved
    sub_off, sub_len,            # SubstituteNameOffset, SubstituteNameLength
    prnt_off, prnt_len,          # PrintNameOffset, PrintNameLength
)
reparse_buf = header + path_buf

# 5. Apply via DeviceIoControl
buf = ctypes.create_string_buffer(reparse_buf)
returned = ctypes.wintypes.DWORD(0)
ok2 = kernel32.DeviceIoControl(
    handle, FSCTL_SET_REPARSE_POINT,
    buf, len(reparse_buf),
    None, 0,
    ctypes.byref(returned), None
)
err2 = kernel32.GetLastError()
kernel32.CloseHandle(handle)
print(f"DeviceIoControl: {ok2}  err={err2}")

if not ok2:
    sys.exit(f"ERROR: DeviceIoControl failed (err={err2})")

# 6. Verify the junction works
monday_via_raw = junction_raw + r'\Monday-WorkingHours.pcap_ISCX.csv'
attr = kernel32.GetFileAttributesW(monday_via_raw)
print(f"Monday attr via raw path: {hex(attr)}  (0xFFFFFFFF = not found)")

# Also test via normal (non-raw) path as replay_reader.py would use it
import os
monday_normal = r'D:\Codingggg\Banglore hackathon\datasets\TrafficLabelling \Monday-WorkingHours.pcap_ISCX.csv'
print(f"Monday via normal path exists: {os.path.exists(monday_normal)}")
