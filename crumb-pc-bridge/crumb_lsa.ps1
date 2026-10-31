<#
  Shared by install_crumb.ps1 and uninstall_crumb.ps1 (dot-sourced).
#>
# Windows keeps the auto sign-in password as an LSA secret ("DefaultPassword"),
# encrypted, readable only by the system - the same place Sysinternals
# Autologon puts it. A $null value deletes it.
if (-not ('CrumbLsa' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class CrumbLsa {
    [StructLayout(LayoutKind.Sequential)]
    struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)]
    struct LSA_OBJECT_ATTRIBUTES { public int Length; public IntPtr RootDirectory; public IntPtr ObjectName; public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
    [DllImport("advapi32.dll")]
    static extern uint LsaOpenPolicy(IntPtr systemName, ref LSA_OBJECT_ATTRIBUTES attrs, uint access, out IntPtr handle);
    [DllImport("advapi32.dll", EntryPoint = "LsaStorePrivateData")]
    static extern uint Store(IntPtr handle, ref LSA_UNICODE_STRING key, ref LSA_UNICODE_STRING data);
    [DllImport("advapi32.dll", EntryPoint = "LsaStorePrivateData")]
    static extern uint Delete(IntPtr handle, ref LSA_UNICODE_STRING key, IntPtr data);
    [DllImport("advapi32.dll")]
    static extern uint LsaClose(IntPtr handle);
    [DllImport("advapi32.dll")]
    static extern int LsaNtStatusToWinError(uint status);
    static LSA_UNICODE_STRING Str(string s) {
        var u = new LSA_UNICODE_STRING();
        u.Buffer = Marshal.StringToHGlobalUni(s);
        u.Length = (ushort)(s.Length * 2);
        u.MaximumLength = (ushort)(s.Length * 2 + 2);
        return u;
    }
    public static void Set(string key, string value) {
        var attrs = new LSA_OBJECT_ATTRIBUTES();
        attrs.Length = Marshal.SizeOf(attrs);
        IntPtr h;
        uint st = LsaOpenPolicy(IntPtr.Zero, ref attrs, 0x000F0FFF, out h);
        if (st != 0) throw new Win32Exception(LsaNtStatusToWinError(st));
        var k = Str(key);
        LSA_UNICODE_STRING v = new LSA_UNICODE_STRING();
        try {
            if (string.IsNullOrEmpty(value)) { st = Delete(h, ref k, IntPtr.Zero); }
            else { v = Str(value); st = Store(h, ref k, ref v); }
            if (st != 0 && !string.IsNullOrEmpty(value)) throw new Win32Exception(LsaNtStatusToWinError(st));
        } finally {
            Marshal.FreeHGlobal(k.Buffer);
            if (v.Buffer != IntPtr.Zero) { Marshal.FreeHGlobal(v.Buffer); }
            LsaClose(h);
        }
    }
}
'@ }
