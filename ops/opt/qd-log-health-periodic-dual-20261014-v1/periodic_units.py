"""New fixed units; never enables the old DAC-only foreign-proc health timer."""
import periodic_contract as c
CAPS='CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER CAP_FSETID CAP_KILL CAP_SETGID CAP_SETUID CAP_SETPCAP CAP_NET_BIND_SERVICE CAP_NET_RAW CAP_SYS_CHROOT CAP_AUDIT_WRITE CAP_SETFCAP'
def properties(child):
    # Default system-manager UID 0 avoids systemd's seccomp UID-transition
    # path dropping SETUID and keeping SETPCAP inheritable. Kernel credentials
    # must still be exact root/zero inheritable and the declared capability set.
    result={'Type':'oneshot','RemainAfterExit':'yes' if child else 'no','User':'','Group':'root','UMask':'0077',
      'MemoryMax':'64M','MemorySwapMax':'0','TasksMax':'32','TimeoutStartSec':'24s','TimeoutStopSec':'2s',
      'CapabilityBoundingSet':'CAP_DAC_READ_SEARCH' if child else CAPS,'AmbientCapabilities':'','NoNewPrivileges':'yes',
      'ProtectSystem':'strict','ProtectHome':'yes' if child else 'read-only','PrivateTmp':'yes','PrivateDevices':'yes','PrivateMounts':'yes',
      'RestrictAddressFamilies':'AF_UNIX','ReadWritePaths':c.CONTROL if child else c.ROOT,
      'InaccessiblePaths':'-/run/qd-approved-alert -/etc/qd-approved-alert -/var/lib/qd-approved-alert'+(' -/var/run/docker.sock' if child else ''),
      'StandardOutput':'null','StandardError':'null','KillMode':'control-group','LimitCORE':'0'}
    if child:result.update(BindReadOnlyPaths=c.SOURCE+':'+c.BOUND,ReadOnlyPaths='/var/lib/qd-redacted-log /var/lib/qd-redacted-log-frontend')
    return result
def unit(child):
    purpose='fixed DAC-only normal canaries' if child else 'fixed periodic read-only frontend verifier'
    props=properties(child);entry=c.CODE+('/child_entry.py' if child else '/root_entry.py')
    return ('[Unit]\nDescription=QuantDinger '+purpose+'\n\n[Service]\n'+
      ''.join(k+'='+v+'\n' for k,v in props.items())+'ExecStart=/usr/bin/python3 -I -B '+entry+'\n').encode()
def timer():
    return ('[Unit]\nDescription=QuantDinger fixed periodic log-health observation\n\n[Timer]\n'
      'OnActiveSec=30s\nOnUnitInactiveSec=30s\nAccuracySec=1s\nPersistent=false\nUnit='+c.ROOT_UNIT+'\n\n[Install]\nWantedBy=timers.target\n').encode()
def files():return {'/etc/systemd/system/'+c.ROOT_UNIT:unit(False),'/etc/systemd/system/'+c.CHILD_UNIT:unit(True),
  '/etc/systemd/system/'+c.TIMER:timer()}
