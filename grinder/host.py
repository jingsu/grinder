# Copyright 2013 GridCentric Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import re
import uuid

from xml.dom.minidom import parseString

from . logger import log
from . shell import RootShell

COBALT_HOOKS_DIR = '/etc/cobalt/hooks.d/'

class CobaltHook:
    def __init__(self, hookname, hookscript, host, config, cleanup = None):
        self.hookname   = hookname + str(uuid.uuid4())
        self.hookscript = hookscript
        self.host       = host
        self.config     = config
        self.cleanup    = cleanup

    def __enter__(self):
        assert self.host.drop_hook(self.hookname, self.hookscript)
        return self

    def __exit__(self, type, value, tb):
        if type == None or not(self.config.leave_on_failure):
            filename = os.path.join(COBALT_HOOKS_DIR, self.hookname)
            self.host.check_output('rm -f %s' % filename)
            if self.cleanup is not None:
                self.host.check_output(self.cleanup)

class Host(object):

    '''The Host object wraps around the HostSecureShell with some
    common operations (getting stats, checking usage, etc.).'''

    def __init__(self, hostname, config):
        self.id = hostname
        self.config = config

    def host_az(self):
        # Note that the host_az is ignored in Essex.
        # To get 100% certainty on the az of a host, we need to use
        # nova-manage. We do not do want to do that and instead impose
        # restrictions based on the az supplied. Eventually nova-manage will go
        # away and we will be able to query az's through novaclient. All will
        # be better then.
        return '%s:%s' % (self.config.default_az, self.id)

    def get_shell(self):
        return RootShell(self.id,
                         self.config.host_key_path,
                         self.config.host_user,
                         self.config.ssh_port)

    def __str__(self):
        return 'Host(id=%s)' % (self.id)

    def check_output(self, command, **kwargs):
        shell = self.get_shell()
        return shell.check_output(command, **kwargs)

    def get_vmsfs_stats(self, genid=None):
        if genid is None:
            path = '/sys/fs/vmsfs/stats'
        else:
            path = '/sys/fs/vmsfs/%s' % genid

        # Grab the stats.
        (stdout, stderr) = self.check_output('cat %s' % path)

        # Post-process.
        lines = [x.strip() for x in stdout.split('\n')]
        statsdict = {}
        for line in lines:
            m = re.match('([a-z_]+): ([0-9]+) -', line)
            (key, value) = m.groups()
            statsdict[key] = long(value)
        return statsdict

    def get_ips(self):
        # Return the list of all assigned IP addresses.
        stdout, stderr = self.check_output('ip addr | grep "inet "')
        ips = map(lambda x: x.split()[1], stdout.split("\n"))
        return [ip.split("/")[0] for ip in ips]

    # Decomposes a chain in the default "filter" table in the host
    # into a list of string repr of rules.
    def __get_iptables_rules(self, chain):
        # Return the list of iptables rules for the specified chain
        stdout, stderr = self.check_output('iptables -n -L %s || true' % chain)
        try:
            rules = stdout.split('\n')[2:]
            if rules[-1] == '':
                rules = rules[:-1]
        except:
            return []

        ips = self.get_ips()
        modified_rules = []
        for rule in rules:
            rule_tokens = rule.split()
            new_rule = []
            for tok in rule_tokens:
                if tok in ips:
                    tok = 'HOST_IP'
                new_rule.append(tok)
            # Re join tokens, clean up spacing
            modified_rules.append(' '.join(new_rule))
        # Sort to rule out comparison false negatives
        modified_rules.sort()
        log.debug("Iptable rules for chain %s on host %s: %s." %
                    (chain, self.id, str(modified_rules)))
        return modified_rules

    # Return the unique string that Neutron uses for tracking VM network devices
    # between libvirt domains, ipchains and neutron networks
    # Return None if not found
    def get_dom_interface_id(self, id):
        virsh_id_string = 'instance-' + hex(id)[2:].zfill(8)
        stdout, stderr = self.check_output('virsh dumpxml %s' % virsh_id_string)
        try:
            doc = parseString(stdout)
            target = doc.getElementsByTagName('interface')[0].getElementsByTagName('target')
            target_dev = target[0].getAttribute('dev')
            # Return just the ID portion of the string ex:
            # target_dev is "tap08e2be78-d8" - return "08e2be78-d8"
            return target_dev[3:]
        except:
            log.error("Failed to parse XML for Virsh domain %s" % virsh_id_string)
            return None

    # Return a (bool, [list]), where bool indicates that a chain
    # for this instance exists in the main filtering chain, and
    # the list contains the rules for the instance chain as per
    # __get_iptables_rules. That way we can catch cases when
    # empty chains are left dangling
    # Note:
    # Grizzly and earlier use the INT(RAW ID) of the dom to identify rules
    # Havana and later uses the tapNNNNN instead of the raw ID to identify rules
    def get_nova_compute_instance_filter_rules(self, iptables_master_rule, server_iptables_chain):
        for rule in self.__get_iptables_rules(iptables_master_rule):
            if server_iptables_chain in rule:
                # This server has rules defined on this host.
                # Grab the server rules for that chain.
                return (True,\
                        self.__get_iptables_rules(server_iptables_chain))

        # No chains and no rules
        return (False, [])

    # Because hooks are not exposed as a capability (and could potentially be
    # enabled on a per compute host basis), do a rudimentary check of the hooks
    # dir (which the cobalt packages lay out with some dumb readmes).
    def check_supports_hooks(self):
        # exc=True causes an exception if rc != 0 (no cobalt hooks dir).
        # If we catch other exceptions, we might as well declare defeat.
        try:
            stdout, stderr = self.check_output('stat %s' %\
                                                COBALT_HOOKS_DIR, exc=True)
        except Exception:
            return False
        return True

    def with_hook(self, hookname, hookscript, cleanup=None):
        return CobaltHook(hookname, hookscript, self, self.config, cleanup)

    def drop_hook(self, hookname, hookscript):
        if not self.check_supports_hooks():
            log.warn("Asked to drop a hook on host %s but it doesn't "
                     "support hooks." % self.id)
            return False
        filename = os.path.join(COBALT_HOOKS_DIR, hookname)
        try:
            self.check_output('touch %s' % filename, exc=True)
            self.check_output('chmod +x %s' % filename, exc=True)
            self.check_output('cat >> %s' % filename, exc=True,
                                input = hookscript)
        except Exception, e:
            log.exception("Dropping hook %s on host %s failed" % hookname, self.id)
            return False
        return True

