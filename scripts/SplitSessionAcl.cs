// Fixed host objects only. Use handles for DACL updates. The Windows object manager
// resolves one component at a time against its pinned parent; no link is followed.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Win32.SafeHandles;

public sealed class SplitSessionAcl : IDisposable
{
    [StructLayout(LayoutKind.Sequential)] struct UnicodeName { public ushort Length, Maximum; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)] struct Attributes {
        public int Length; public IntPtr Root, Name; public int Flags; public IntPtr Descriptor, Quality;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoStatus { public IntPtr Status, Information; }
    [StructLayout(LayoutKind.Sequential)] struct FileInfo {
        public uint Attributes; public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("ntdll.dll")] static extern int NtOpenFile(out SafeFileHandle handle, uint rights,
        ref Attributes attributes, out IoStatus status, uint share, uint options);
    [DllImport("ntdll.dll")] static extern int NtSetSecurityObject(SafeFileHandle handle, uint info, byte[] descriptor);
    [DllImport("ntdll.dll")] static extern int NtQuerySecurityObject(SafeFileHandle handle, uint info, byte[] descriptor,
        int size, out int needed);
    [DllImport("ntdll.dll")] static extern int RtlNtStatusToDosError(int status);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetFileInformationByHandle(
        SafeFileHandle handle, out FileInfo info);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern SafeFileHandle CreateFile(
        string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr OpenSCManager(
        string machine,string database,uint access);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr OpenService(
        IntPtr manager,string service,uint access);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool QueryServiceObjectSecurity(
        IntPtr service,uint info,byte[] bytes,int length,out int needed);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool SetServiceObjectSecurity(IntPtr service,uint info,byte[] bytes);
    [DllImport("advapi32.dll")] static extern bool CloseServiceHandle(IntPtr service);

    const string Host = @"C:\ProgramData\AI-Agent-Office";
    readonly string auditPath;
    readonly SecurityIdentifier grant;
    readonly HashSet<string> trustedOwners;
    readonly HashSet<string> expiredGroups = new HashSet<string>();
    readonly Dictionary<string,SafeFileHandle> anchors = new Dictionary<string,SafeFileHandle>();
    public SplitSessionAcl(string log, string group, string human) {
        auditPath = log; grant = new SecurityIdentifier(group);
        trustedOwners = new HashSet<string> { human, "S-1-5-18", "S-1-5-32-544" };
        var drive = CreateFile(@"\\?\C:\", 0x80, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
        if (drive.IsInvalid) throw new Win32Exception();
        anchors.Add(@"C:\", drive);
        try {
            foreach (string path in new [] { @"C:\ProgramData", Host }) {
                var h = Child(anchors[Path.GetDirectoryName(path)], Path.GetFileName(path), path==Host);
                anchors.Add(path,h);
                if ((Info(h).Attributes & 16)==0) throw new IOException("Expected directory: "+path);
            }
        } catch { Dispose(); throw; }
    }
    public void AcceptRetiredSession(string owner,string group) {
        // Caller supplies only durable, administrator-owned completion receipts.
        trustedOwners.Add(new SecurityIdentifier(owner).Value);
        expiredGroups.Add(new SecurityIdentifier(group).Value);
    }
    static void Check(int status) {
        if (status < 0) throw new Win32Exception(RtlNtStatusToDosError(status));
    }
    static FileInfo Info(SafeFileHandle h) {
        FileInfo info;
        if (!GetFileInformationByHandle(h, out info)) throw new Win32Exception();
        if ((info.Attributes & 1024)!=0 || ((info.Attributes & 16)==0 && info.Links!=1))
            throw new IOException("Links are not eligible for a permission grant");
        return info;
    }
    static SafeFileHandle Child(SafeFileHandle parent, string name, bool write) {
        if (String.IsNullOrEmpty(name) || name=="." || name==".." || name.IndexOfAny(new [] {'\\','/',':','\0'})>=0)
            throw new IOException("Invalid file component");
        IntPtr value = Marshal.StringToHGlobalUni(name);
        IntPtr pointer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeName)));
        try {
            var text = new UnicodeName { Length=checked((ushort)(2*name.Length)),
                Maximum=checked((ushort)(2*name.Length+2)), Buffer=value };
            Marshal.StructureToPtr(text, pointer, false);
            var attrs = new Attributes { Length=Marshal.SizeOf(typeof(Attributes)), Root=parent.DangerousGetHandle(),
                Name=pointer, Flags=0x1040 }; // OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE
            SafeFileHandle result; IoStatus status;
            Check(NtOpenFile(out result, (uint)(0x20080 | (write ? 0x40000 : 0)), ref attrs, out status, 3, 0x200000));
            try { Info(result); return result; } catch { result.Dispose(); throw; }
        } finally { GC.KeepAlive(parent); Marshal.FreeHGlobal(pointer); Marshal.FreeHGlobal(value); }
    }
    static byte[] Descriptor(SafeFileHandle handle) {
        int needed;
        // OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        NtQuerySecurityObject(handle,5,null,0,out needed);
        if (needed<=0 || needed>262144) throw new IOException("Invalid security descriptor size");
        byte[] bytes = new byte[needed];
        Check(NtQuerySecurityObject(handle,5,bytes,bytes.Length,out needed));
        return bytes;
    }
    static string Identity(FileInfo info) {
        return String.Format("{0:x8}{1:x8}{2:x8}",info.Volume,info.IndexHigh,info.IndexLow);
    }
    static byte[] Bytes(GenericSecurityDescriptor sd) {
        var bytes = new byte[sd.BinaryLength]; sd.GetBinaryForm(bytes,0); return bytes;
    }
    static string Access(byte[] bytes) {
        return new RawSecurityDescriptor(bytes,0).GetSddlForm(AccessControlSections.Access);
    }
    static void SetDacl(SafeFileHandle h,byte[] persisted) {
        // NTFS preserves the auto-inherited flag on a native DACL write only when
        // the matching auto-inherit request bit is supplied to the object manager.
        var request=(byte[])persisted.Clone();
        if((request[3]&4)!=0) request[3]|=1;
        Check(NtSetSecurityObject(h,4,request));
    }
    void Grant(SafeFileHandle h, string path, FileInfo info, int mask, bool inherit, bool newChildren=false) {
        var before=Descriptor(h);
        var sd=new RawSecurityDescriptor(before,0);
        if (sd.DiscretionaryAcl==null || !trustedOwners.Contains(sd.Owner.Value))
            throw new IOException("Unexpected owner or missing DACL: "+path);
        int index=0;
        foreach (GenericAce ace in sd.DiscretionaryAcl) {
            var common=ace as CommonAce;
            if (common==null || common.IsCallback) throw new IOException("Unsupported ACE: "+path);
            if (common.SecurityIdentifier.Equals(grant)) throw new IOException("Unexpected existing session ACE");
            var sid=common.SecurityIdentifier.Value;
            if (common.AceQualifier==AceQualifier.AccessAllowed && (common.AccessMask & 0x500d0156)!=0 &&
                !trustedOwners.Contains(sid) && !expiredGroups.Contains(sid) && sid!="S-1-3-4" && sid!="S-1-3-0" &&
                !((path==Host+@"\data" || path.StartsWith(Host+@"\data\",StringComparison.Ordinal)) &&
                  sid==((SecurityIdentifier)new NTAccount(@"NT SERVICE\AgentOfficeStudio").Translate(typeof(SecurityIdentifier))).Value))
                throw new IOException("Untrusted write permission: "+path);
            if ((ace.AceFlags&AceFlags.Inherited)==0 && common.AceQualifier==AceQualifier.AccessDenied) index++;
        }
        var flags=inherit ? AceFlags.ContainerInherit|AceFlags.ObjectInherit : AceFlags.None;
        sd.DiscretionaryAcl.InsertAce(index,new CommonAce(flags,AceQualifier.AccessAllowed,mask,grant,false,null));
        if(newChildren)
            sd.DiscretionaryAcl.InsertAce(index+1,new CommonAce(AceFlags.InheritOnly|AceFlags.ObjectInherit|AceFlags.ContainerInherit,
                AceQualifier.AccessAllowed,0x1301bf,grant,false,null));
        var after=Bytes(sd);
        string record=String.Join("\t",new [] {
            Convert.ToBase64String(Encoding.Unicode.GetBytes(path)),Identity(info),
            Convert.ToBase64String(before),Convert.ToBase64String(after),"\n" });
        using(var stream=new FileStream(auditPath,FileMode.Append,FileAccess.Write,FileShare.Read,4096,FileOptions.WriteThrough)) {
            var bytes=Encoding.ASCII.GetBytes(record); stream.Write(bytes,0,bytes.Length); stream.Flush(true);
        }
        SetDacl(h,after);
        if (Access(Descriptor(h))!=Access(after)) throw new IOException("Cannot verify applied DACL: "+path);
    }
    void Tree(SafeFileHandle parent,string path,int mask,bool recurse,bool root) {
        using(var h=Child(parent,Path.GetFileName(path),true)) {
            var info=Info(h); bool dir=(info.Attributes&16)!=0;
            if (root && recurse && !dir) throw new IOException("Expected directory: "+path);
            // Add inheritance to the directory for new children. NtSetSecurityObject
            // updates this handle only, so existing entries are audited individually.
            Grant(h,path,info,root && recurse ? mask & ~0x10000 : mask,dir && recurse);
            if (dir && recurse) foreach(string child in Directory.EnumerateFileSystemEntries(path))
                Tree(h,child,mask,true,false);
        }
    }
    void ObjectStore(SafeFileHandle parent,string path) {
        using(var repo=Child(parent,"objects.git",true)) {
            Grant(repo,path,Info(repo),0x1200a9,false);
            foreach(string leaf in new[]{"config","HEAD","packed-refs","shallow"}) {
                try { Tree(repo,Path.Combine(path,leaf),0x120089,false,false); }
                catch(Win32Exception e) { if(e.NativeErrorCode!=2) throw; }
            }
            using(var objects=Child(repo,"objects",true)) {
                string objectPath=Path.Combine(path,"objects");
                Grant(objects,objectPath,Info(objects),0x1200ad,false,true);
                foreach(string namespacePath in Directory.EnumerateDirectories(objectPath)) {
                    var folder=Path.GetFileName(namespacePath);
                    if(folder=="info") { Tree(objects,namespacePath,0x1200a9,true,true); continue; }
                    if(folder!="pack" && !Regex.IsMatch(folder,@"\A[0-9a-f]{2}\z")) continue;
                    using(var dest=Child(objects,folder,true)) {
                        Grant(dest,namespacePath,Info(dest),0x1200ab,false,true);
                        foreach(string entry in Directory.EnumerateFiles(namespacePath))
                            Tree(dest,entry,0x120089,false,false);
                    }
                }
            }
            using(var refs=Child(repo,"refs",true)) {
                string refPath=Path.Combine(path,"refs");
                Grant(refs,refPath,Info(refs),0x1200a9,false);
                using(var required=Child(refs,"candidates",false)) {
                    if((Info(required).Attributes&16)==0) throw new IOException("Expected candidates ref directory");
                }
                foreach(string entry in Directory.EnumerateDirectories(refPath)) {
                    if(Path.GetFileName(entry)!="candidates") { Tree(refs,entry,0x1200a9,true,true); continue; }
                    using(var candidates=Child(refs,"candidates",true)) {
                        Grant(candidates,entry,Info(candidates),0x1200ab,false,true);
                        foreach(string file in Directory.EnumerateFiles(entry))
                            Tree(candidates,file,0x120089,false,false);
                    }
                }
            }
        }
    }
    public void Provision() {
        var root=anchors[Host];
        // A worker must not be able to swap any of the four controller/ledger roots.
        var parent=new RawSecurityDescriptor(Descriptor(root),0);
        if(parent.DiscretionaryAcl==null || !trustedOwners.Contains(parent.Owner.Value))
            throw new IOException("Unexpected host root owner or DACL");
        foreach(GenericAce ace in parent.DiscretionaryAcl) {
            var rule=ace as CommonAce;
            if(rule==null || rule.IsCallback) throw new IOException("Unsupported host root DACL");
            if(rule.AceQualifier==AceQualifier.AccessAllowed && (rule.AccessMask & 0x500d0156)!=0 &&
                !trustedOwners.Contains(rule.SecurityIdentifier.Value) && rule.SecurityIdentifier.Value!="S-1-3-4")
                throw new IOException("Unexpected host root write permission");
        }
        Grant(root,Host,Info(root),0x200a0,false);
        foreach(string name in new [] {"release","deploy"}) {
            string dir=Path.Combine(Host,name);
            using(var h=Child(root,name,true)) {
                Grant(h,dir,Info(h),0x1200af,false,true); // create names, no deletion of existing children
                foreach(var child in Directory.EnumerateFileSystemEntries(dir)) {
                    string leaf=Path.GetFileName(child);
                    if(name=="deploy" && leaf=="slots") {
                        using(var slots=Child(h,"slots",true)) {
                            Grant(slots,child,Info(slots),0x1200ad,false,true); // new subdirectories only
                            foreach(var existing in Directory.EnumerateFileSystemEntries(child))
                                Tree(slots,existing,0x1200a9,true,true);
                        }
                    } else if(leaf=="deployment.sqlite3" && name=="deploy" || leaf=="release.sqlite3" && name=="release" ||
                              leaf=="operator-policy.sqlite3" && name=="release") {
                        Tree(h,child,0x12019f,false,false);
                    } else if(leaf=="objects.git" && name=="release") {
                        ObjectStore(h,child);
                    } else if(leaf=="deployment.sqlite3.locks" && name=="deploy" ||
                              (leaf=="release.sqlite3.locks" || leaf=="operator-policy.sqlite3.locks") && name=="release") {
                        Tree(h,child,0x1301bf,true,true);
                    } else {
                        throw new IOException("Unexpected ledger entry; retry after active operation finishes: "+child);
                    }
                }
            }
        }
        string data=Path.Combine(Host,"data");
        using(var h=Child(root,"data",true)) {
            Grant(h,data,Info(h),0x200a0,false);
            using(var db=Child(h,"tasks.sqlite3",true)) Grant(db,Path.Combine(data,"tasks.sqlite3"),Info(db),0x12019f,false);
            using(var locks=Child(h,"tasks.sqlite3.locks",true)) {
                Grant(locks,Path.Combine(data,"tasks.sqlite3.locks"),Info(locks),0x200a0,false);
                const string name="studio-improvement-generation.lock";
                try {
                    using(var file=Child(locks,name,true))
                        Grant(file,Path.Combine(data,"tasks.sqlite3.locks",name),Info(file),0x120089,false);
                } catch(Win32Exception e) { if(e.NativeErrorCode!=2) throw; }
            }
        }
    }
    static byte[] ServiceDescriptor(IntPtr service) {
        int length; QueryServiceObjectSecurity(service,4,null,0,out length);
        if(length<=0 || length>65536) throw new Win32Exception();
        var bytes=new byte[length];
        if(!QueryServiceObjectSecurity(service,4,bytes,length,out length)) throw new Win32Exception();
        return bytes;
    }
    public void Service(bool restore) {
        string path=auditPath+".service";
        if(restore && !File.Exists(path)) return;
        var manager=OpenSCManager(null,null,1);
        if(manager==IntPtr.Zero) throw new Win32Exception();
        try {
            var service=OpenService(manager,"AgentOfficeStudio",0x60000);
            if(service==IntPtr.Zero) throw new Win32Exception();
            try {
                var before=ServiceDescriptor(service); byte[] apply;
                if(restore) {
                    var fields=File.ReadAllLines(path);
                    if(fields.Length!=2) throw new IOException("Incomplete service DACL recovery record");
                    apply=Convert.FromBase64String(fields[0]);
                    if(Access(before)==Access(apply)) return;
                    if(Access(before)!=Access(Convert.FromBase64String(fields[1])))
                        throw new IOException("Service DACL changed concurrently");
                } else {
                    var sd=new RawSecurityDescriptor(before,0);
                    if(sd.DiscretionaryAcl==null) throw new IOException("Missing service DACL");
                    int index=0;
                    foreach(GenericAce ace in sd.DiscretionaryAcl) {
                        var common=ace as CommonAce;
                        if(common==null || common.IsCallback || common.SecurityIdentifier.Equals(grant))
                            throw new IOException("Unsupported existing service ACE");
                        if(common.AceQualifier==AceQualifier.AccessDenied) index++;
                    }
                    // SERVICE_QUERY_CONFIG | SERVICE_QUERY_STATUS | SERVICE_START | SERVICE_STOP.
                    sd.DiscretionaryAcl.InsertAce(index,new CommonAce(0,AceQualifier.AccessAllowed,0x35,grant,false,null));
                    apply=Bytes(sd);
                    using(var stream=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.Read,4096,FileOptions.WriteThrough)) {
                        var record=Encoding.ASCII.GetBytes(Convert.ToBase64String(before)+"\n"+Convert.ToBase64String(apply)+"\n");
                        stream.Write(record,0,record.Length); stream.Flush(true);
                    }
                }
                if(!SetServiceObjectSecurity(service,4,apply)) throw new Win32Exception();
                if(Access(ServiceDescriptor(service))!=Access(apply)) throw new IOException("Service DACL verification failed");
            } finally { CloseServiceHandle(service); }
        } finally { CloseServiceHandle(manager); }
    }
    public void Recover() {
        if (!File.Exists(auditPath)) return;
        var records=File.ReadAllLines(auditPath,Encoding.ASCII);
        var errors=new List<string>();
        for(int i=records.Length-1;i>=0;i--) {
            try {
                var fields=records[i].TrimEnd('\t').Split('\t');
                if(fields.Length!=4) throw new IOException("Incomplete undo record");
                var path=Encoding.Unicode.GetString(Convert.FromBase64String(fields[0]));
                if (path!=Host && !path.StartsWith(Host+"\\",StringComparison.Ordinal)) throw new IOException("Unexpected undo path");
                var parts=path==Host ? new string[0] : path.Substring(Host.Length+1).Split('\\');
                var owned=new List<SafeFileHandle>();
                try {
                    var current=anchors[Host];
                    for(int j=0;j<parts.Length;j++){
                        current=Child(current,parts[j],j==parts.Length-1); owned.Add(current);
                    }
                    if(Identity(Info(current))!=fields[1]) throw new IOException("Object identity changed: "+path);
                    var before=Convert.FromBase64String(fields[2]); var after=Convert.FromBase64String(fields[3]);
                    var present=Access(Descriptor(current));
                    if (present==Access(before)) continue;
                    if (present!=Access(after)) throw new IOException("DACL changed concurrently: "+path);
                    SetDacl(current,before);
                    if(Access(Descriptor(current))!=Access(before)) throw new IOException("DACL restoration failed: "+path);
                } finally { for(int j=owned.Count-1;j>=0;j--) owned[j].Dispose(); }
            } catch(Exception e) { errors.Add(e.Message); }
        }
        if(errors.Count!=0) throw new IOException("Incomplete ACL recovery: "+String.Join(" | ",errors));
    }
    public void Dispose() { foreach(var h in anchors.Values) h.Dispose(); anchors.Clear(); }
}
