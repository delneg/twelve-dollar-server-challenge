// Layout test: compares what Transport.fs and the built Server.dll assume about native structs and constants
// with what the C headers say on this box (native_layout.c output). Exit 1 on any mismatch.
// Usage: dotnet fsi layout_check.fsx <out/Server.dll> <native_layout output file> <Transport.fs>
open System
open System.IO
open System.Reflection
open System.Runtime.InteropServices
open System.Text.RegularExpressions

let args = fsi.CommandLineArgs |> Array.skip 1
let dll, nativeOut, transportFs = args.[0], args.[1], args.[2]
let c =
    File.ReadAllLines nativeOut
    |> Array.filter (fun l -> l.Trim() <> "")
    |> Array.map (fun l -> let p = l.Split ' ' in p.[0], int64 p.[1])
    |> dict
let mutable failures = 0
let check name (expected: int64) (actual: int64) =
    if expected = actual then printfn "PASS %-34s %d" name actual
    else
        failures <- failures + 1
        printfn "FAIL %-34s header=%d ours=%d" name expected actual

printfn "arch=%O os=%s little-endian=%b" RuntimeInformation.ProcessArchitecture RuntimeInformation.OSDescription BitConverter.IsLittleEndian

// 1. struct epoll_event as the built binary lays it out: for P/Invoke (Marshal) and in a managed array (the
//    256-entry array that epoll_wait fills), which is what the kernel writes into.
let asm = Assembly.LoadFrom(Path.GetFullPath dll)
let t = asm.GetType("Transport+EpollEvent", true)
let unsafeSizeOf =
    typeof<System.Runtime.CompilerServices.Unsafe>.GetMethod("SizeOf").MakeGenericMethod([| t |]).Invoke(null, [||]) :?> int
check "sizeof epoll_event (Marshal)" c.["sizeof_epoll_event"] (int64 (Marshal.SizeOf t))
check "sizeof epoll_event (array stride)" c.["sizeof_epoll_event"] (int64 unsafeSizeOf)
check "offsetof epoll_event.events" c.["offsetof_epoll_event_events"] (int64 (Marshal.OffsetOf(t, "Events")))
check "offsetof epoll_event.data" c.["offsetof_epoll_event_data"] (int64 (Marshal.OffsetOf(t, "Data")))
// 2. sockaddr_in, which Transport.fs builds by hand: 16 bytes, family (u16, little-endian) at 0, port at 2, addr at 4.
check "sizeof sockaddr_in" c.["sizeof_sockaddr_in"] 16L
check "offsetof sin_family" c.["offsetof_sockaddr_in_family"] 0L
check "sizeof sin_family" c.["sizeof_sockaddr_in_family"] 2L
check "offsetof sin_port" c.["offsetof_sockaddr_in_port"] 2L
check "offsetof sin_addr" c.["offsetof_sockaddr_in_addr"] 4L
check "little-endian (family byte 0)" 1L (if BitConverter.IsLittleEndian then 1L else 0L)

// 3. P/Invoke widths: recv/send/read/write use nativeint for size_t/ssize_t, bind/setsockopt pass int lengths.
check "sizeof ssize_t = nativeint" c.["sizeof_ssize_t"] (int64 IntPtr.Size)
check "sizeof size_t = nativeint" c.["sizeof_size_t"] (int64 IntPtr.Size)
check "sizeof socklen_t = int" c.["sizeof_socklen_t"] 4L
check "sizeof int" c.["sizeof_int"] 4L
check "sizeof eventfd_t (read/write 8n)" c.["sizeof_eventfd_t"] 8L

// 4. Constants hard-coded in Transport.fs.
let src = File.ReadAllText transportFs
let ours =
    Regex.Matches(src, @"let private ([A-Z_]+) = (0x[0-9A-Fa-f]+|\d+)u?\b")
    |> Seq.map (fun m -> m.Groups.[1].Value, Convert.ToInt64(m.Groups.[2].Value, (if m.Groups.[2].Value.StartsWith "0x" then 16 else 10)))
    |> dict
for KeyValue(name, v) in ours do
    if c.ContainsKey name then check ("const " + name) c.[name] v
    else printfn "SKIP const %s (not in native_layout.c)" name
// Flags used under another name.
check "EPOLL_CLOEXEC = SOCK_CLOEXEC" c.["EPOLL_CLOEXEC"] ours.["SOCK_CLOEXEC"]
check "EFD_NONBLOCK = SOCK_NONBLOCK" c.["EFD_NONBLOCK"] ours.["SOCK_NONBLOCK"]
check "EFD_CLOEXEC = SOCK_CLOEXEC" c.["EFD_CLOEXEC"] ours.["SOCK_CLOEXEC"]
check "SHUT_RDWR (shutdown(fd, 2))" c.["SHUT_RDWR"] (if src.Contains "Libc.shutdown(fd, 2)" then 2L else -1L)

printfn "%s: %d failure(s)" (if failures = 0 then "LAYOUT OK" else "LAYOUT FAILED") failures
exit (if failures = 0 then 0 else 1)
