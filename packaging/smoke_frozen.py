"""Exercise a frozen executable without opening visible windows or a browser."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import ctypes
from ctypes import wintypes
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtNetwork import QLocalSocket

exe=Path(sys.argv[1]).resolve()
app=QCoreApplication([])

def stop_children(parent):
    class Entry(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('usage',wintypes.DWORD),('pid',wintypes.DWORD),
                  ('heap',ctypes.c_size_t),('module',wintypes.DWORD),('threads',wintypes.DWORD),
                  ('parent',wintypes.DWORD),('priority',wintypes.LONG),('flags',wintypes.DWORD),
                  ('name',wintypes.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    entry=Entry(); entry.size=ctypes.sizeof(entry)
    pairs=[]
    more=kernel.Process32FirstW(snapshot,ctypes.byref(entry))
    while more:
        pairs.append((entry.pid,entry.parent))
        more=kernel.Process32NextW(snapshot,ctypes.byref(entry))
    kernel.CloseHandle(snapshot)
    owned=[parent]
    for pid in owned:
        owned.extend(child for child,ppid in pairs if ppid==pid and child not in owned)
    for pid in reversed(owned[1:]):
        handle=kernel.OpenProcess(1,False,pid)
        if handle:
            kernel.TerminateProcess(handle,0)
            kernel.CloseHandle(handle)
base=Path(__file__).resolve().parent.parent/'build'/'smoke'
base.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='frozen-',dir=base) as folder:
    work=Path(folder)
    assert work.resolve().is_relative_to(base.resolve())
    env=os.environ.copy()
    env.update(QT_QPA_PLATFORM='offscreen',USERPROFILE=str(work),TEMP=str(work),TMP=str(work),PYTHONPATH='')
    env['PATH']=os.path.join(os.environ.get('SystemRoot',os.environ.get('SYSTEMROOT','C:\\Windows')),'System32')
    name='xStack-files-'+hashlib.sha256(str(work).encode('utf-8')).hexdigest()[:20]
    data=work/'sample.xy'
    data.write_text('\n'.join(f'{i/10} {10+(i%7)**2}' for i in range(100,400)))
    def send(paths):
        socket=QLocalSocket()
        socket.connectToServer(name)
        if not socket.waitForConnected(250):
            return False
        socket.write(json.dumps(paths).encode()+b'\n')
        socket.waitForBytesWritten(1000)
        ready=socket.bytesAvailable() or socket.waitForReadyRead(5000)
        reply=bytes(socket.readAll()) if ready else b''
        socket.disconnectFromServer()
        assert reply==b'OK\n',reply
        return True
    log=work/'process.log'
    with log.open('wb') as out:
        process=subprocess.Popen([str(exe)],cwd=work,env=env,stdout=out,stderr=out,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                assert process.poll() is None, f'Unexpected exit {process.returncode}'
                current_log=log.read_text(errors='replace')
                assert 'Traceback' not in current_log,current_log
                if send([]): break
                time.sleep(.25)
            else: raise AssertionError('Frozen startup timed out')
            # Let the two-second splash complete and construct the main window.
            time.sleep(4)
            assert process.poll() is None
            assert send([str(data)])
            time.sleep(2)
            forwarded=subprocess.run([str(exe),str(data)],cwd=work,env=env,stdout=out,stderr=out,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
            assert forwarded.returncode==0,forwarded.returncode
            assert process.poll() is None
        finally:
            stop_children(process.pid)
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait(timeout=15)
    output=log.read_text(errors='replace')
    assert 'Traceback' not in output,output
    report={'exe':str(exe),'startup':True,'file_import_request':True,'second_launch_forwarding':True,'python_path_removed':True,'log':output}
    target=base/(exe.stem+'-report.json')
    target.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='log'},indent=2))
