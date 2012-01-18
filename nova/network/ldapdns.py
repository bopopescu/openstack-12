# Copyright 2012 Andrew Bogott for the Wikimedia Foundation
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

import ldap
import os
import shutil
import tempfile
import time

from nova.auth import fakeldap
from nova import exception
from nova import flags
from nova import log as logging


LOG = logging.getLogger("nova.network.manager")

flags.DEFINE_string('ldap_dns_url',
                    'ldap://ldap.example.com:389',
                    'URL for ldap server which will store dns entries')
flags.DEFINE_string('ldap_dns_user',
                    'uid=admin,ou=people,dc=example,dc=org',
                    'user for ldap DNS')
flags.DEFINE_string('ldap_dns_password',
                    'password',
                    'password for ldap DNS')
flags.DEFINE_string('ldap_dns_soa_hostmaster',
                    'hostmaster@example.org',
                    'Hostmaster for ldap dns driver Statement of Authority')
flags.DEFINE_multistring('ldap_dns_servers',
                    '[dns.example.org]',
                    'DNS Servers for ldap dns driver')
flags.DEFINE_string('ldap_dns_base_dn',
                    'ou=hosts,dc=example,dc=org',
                    'Base DN for DNS entries in ldap')
flags.DEFINE_string('ldap_dns_soa_refresh',
                    '1800',
                    'Refresh interval (in seconds) for ldap dns driver '
                    'Statement of Authority')
flags.DEFINE_string('ldap_dns_soa_retry',
                    '3600',
                    'Retry interval (in seconds) for ldap dns driver '
                    'Statement of Authority')
flags.DEFINE_string('ldap_dns_soa_expiry',
                    '86400',
                    'Expiry interval (in seconds) for ldap dns driver '
                    'Statement of Authority')
flags.DEFINE_string('ldap_dns_soa_minimum',
                    '7200',
                    'Minimum interval (in seconds) for ldap dns driver '
                    'Statement of Authority')


# Importing ldap.modlist breaks the tests for some reason,
#  so this is an abbreviated version of a function from
#  there.
def create_modlist(newattrs):
    modlist = []
    for attrtype in newattrs.keys():
        modlist.append((attrtype, newattrs[attrtype]))
    return modlist


class DNSEntry(object):

    def __init__(self, ldap_object):
        """ldap_object is an instance of ldap.LDAPObject.
           It should already be initialized and bound before
           getting passed in here."""
        self.lobj = ldap_object
        self.ldap_tuple = None
        self.qualified_domain = None

    @classmethod
    def _get_tuple_for_domain(cls, lobj, domain):
        entry = lobj.search_s(flags.FLAGS.ldap_dns_base_dn, ldap.SCOPE_SUBTREE,
                              "(associatedDomain=%s)" % domain)
        if not entry:
            return None
        if len(entry) > 1:
            LOG.warn("Found multiple matches for domain %s.\n%s" %
                     (domain, entry))
        return entry[0]

    def _set_tuple(self, tuple):
        self.ldap_tuple = tuple

    def _qualify(self, name):
        return "%s.%s" % (name, self.qualified_domain)

    def _dequalify(self, name):
        z = ".%s" % self.qualified_domain
        if name.endswith(z):
            dequalified = name[0:name.rfind(z)]
        else:
            LOG.warn("Unable to dequalify.  %s is not in %s.\n" % (name, zone))
            dequalified = None

        return dequalified

    def _dn(self):
        return self.ldap_tuple[0]
    dn = property(_dn)

    def _rdn(self):
        return self.dn.partition(',')[0]
    rdn = property(_rdn)


class DomainEntry(DNSEntry):

    @classmethod
    def _soa(cls):
        date = time.strftime("%Y%m%d%H%M%S")
        soa = "%s %s %s %s %s %s %s" % (
                 flags.FLAGS.ldap_dns_servers[0],
                 flags.FLAGS.ldap_dns_soa_hostmaster,
                 date,
                 flags.FLAGS.ldap_dns_soa_refresh,
                 flags.FLAGS.ldap_dns_soa_retry,
                 flags.FLAGS.ldap_dns_soa_expiry,
                 flags.FLAGS.ldap_dns_soa_minimum)
        return soa

    @classmethod
    def create_domain(cls, lobj, fqdomain):
        """Create a new domain entry, and return an object that wraps it."""
        entry = cls._get_tuple_for_domain(lobj, fqdomain)
        if entry:
            raise exception.FloatingIpDNSExists(name=fqdomain, zone="")

        newdn = "dc=%s,%s" % (fqdomain, flags.FLAGS.ldap_dns_base_dn)
        attrs = {'objectClass': ['domainrelatedobject', 'dnsdomain',
                                 'domain', 'dcobject', 'top'],
                 'sOARecord': [cls._soa()],
                 'associatedDomain': [fqdomain],
                 'dc': fqdomain}
        lobj.add_s(newdn, create_modlist(attrs))
        return DomainEntry(lobj, fqdomain)

    def __init__(self, ldap_object, domain):
        super(DomainEntry, self).__init__(ldap_object)
        entry = self._get_tuple_for_domain(self.lobj, domain)
        if not entry:
            raise exception.NotFound()
        self._set_tuple(entry)
        assert(entry[1]['associatedDomain'][0] == domain)
        self.qualified_domain = domain

    def delete(self):
        """Delete the domain that this entry refers to."""
        entries = self.lobj.search_s(self.dn,
                                     ldap.SCOPE_SUBTREE,
                                     '(aRecord=*)')
        for entry in entries:
            self.lobj.delete_s(entry[0])

        self.lobj.delete_s(self.dn)

    def update_soa(self):
        mlist = [(ldap.MOD_REPLACE, 'sOARecord', self._soa())]
        self.lobj.modify_s(self.dn, mlist)

    def subentry_with_name(self, name):
        entry = self.lobj.search_s(self.dn, ldap.SCOPE_SUBTREE,
                                   "(associatedDomain=%s.%s)" %
                                     (name, self.qualified_domain))
        if entry:
            return HostEntry(self, entry[0])
        else:
            return None

    def subentries_with_ip(self, ip):
        entries = self.lobj.search_s(self.dn, ldap.SCOPE_SUBTREE,
                                   "(aRecord=%s)" % ip)
        objs = []
        for entry in entries:
            if 'associatedDomain' in entry[1]:
                objs.append(HostEntry(self, entry))

        return objs

    def add_entry(self, name, address):
        if self.subentry_with_name(name):
            raise exception.FloatingIpDNSExists(name=name,
                                                zone=self.qualified_domain)

        entries = self.subentries_with_ip(address)
        if entries:
            # We already have an ldap entry for this IP, so we just
            # need to add the new name.
            existingdn = entries[0].dn
            self.lobj.modify_s(existingdn, [(ldap.MOD_ADD,
                                            'associatedDomain',
                                             self._qualify(name))])
            return self.subentry_with_name(name)
        else:
            # We need to create an entirely new entry.
            newdn = "dc=%s,%s" % (name, self.dn)
            attrs = {'objectClass': ['domainrelatedobject', 'dnsdomain',
                                     'domain', 'dcobject', 'top'],
                     'aRecord': [address],
                     'associatedDomain': [self._qualify(name)],
                     'dc': name}
            self.lobj.add_s(newdn, create_modlist(attrs))
            return self.subentry_with_name(name)
        self.update_soa()

    def remove_entry(self, name):
        entry = self.subentry_with_name(name)
        if not entry:
            raise exception.NotFound()
        entry.remove_name(name)
        self.update_soa()


class HostEntry(DNSEntry):

    def __init__(self, parent, tuple):
        super(HostEntry, self).__init__(parent.lobj)
        self.parent_entry = parent
        self._set_tuple(tuple)
        self.qualified_domain = parent.qualified_domain

    def remove_name(self, name):
        names = self.ldap_tuple[1]['associatedDomain']
        if not names:
            raise exception.NotFound()
        if len(names) > 1:
            # We just have to remove the requested domain.
            self.lobj.modify_s(self.dn, [(ldap.MOD_DELETE, 'associatedDomain',
                                         self._qualify(name))])
            if (self.rdn[1] == name):
                # We just removed the rdn, so we need to move this entry.
                names.remove(self._qualify(name))
                newrdn = "dc=%s" % self._dequalify(names[0])
                self.lobj.modrdn_s(self.dn, newrdn)
        else:
            # We should delete the entire record.
            self.lobj.delete_s(self.dn)

    def modify_address(self, name, address):
        names = self.ldap_tuple[1]['associatedDomain']
        if not names:
            raise exception.NotFound()
        if len(names) == 1:
            self.lobj.modify_s(self.dn, [(ldap.MOD_REPLACE, 'aRecord',
                                         [address])])
        else:
            self.remove_name(name)
            parent.add_entry(name, address)

    def _names(self):
        names = []
        for domain in self.ldap_tuple[1]['associatedDomain']:
            names.append(self._dequalify(domain))
        return names
    names = property(_names)

    def _ip(self):
        ip = self.ldap_tuple[1]['aRecord'][0]
        return ip
    ip = property(_ip)

    def _parent(self):
        return self.parent_entry
    parent = property(_parent)


class LdapDNS(object):
    """Driver for PowerDNS using ldap as a back end.

       This driver assumes ldap-method=strict, with all domains
       in the top-level, aRecords only."""

    def __init__(self):
        self.lobj = ldap.initialize(flags.FLAGS.ldap_dns_url)
        self.lobj.simple_bind_s(flags.FLAGS.ldap_dns_user,
                                flags.FLAGS.ldap_dns_password)

    def get_zones(self):
        return flags.FLAGS.floating_ip_dns_zones

    def create_entry(self, name, address, type, dnszone):
        if type.lower() != 'a':
            raise exception.InvalidInput(_("This driver only supports "
                                           "type 'a' entries."))

        dEntry = DomainEntry(self.lobj, dnszone)
        dEntry.add_entry(name, address)

    def delete_entry(self, name, dnszone):
        dEntry = DomainEntry(self.lobj, dnszone)
        dEntry.remove_entry(name)

    def get_entries_by_address(self, address, dnszone):
        try:
            dEntry = DomainEntry(self.lobj, dnszone)
        except exception.NotFound:
            return []
        entries = dEntry.subentries_with_ip(address)
        names = []
        for entry in entries:
            names.extend(entry.names)
        return names

    def get_entries_by_name(self, name, dnszone):
        try:
            dEntry = DomainEntry(self.lobj, dnszone)
        except exception.NotFound:
            return []
        nEntry = dEntry.subentry_with_name(name)
        if nEntry:
            return [nEntry.ip]

    def modify_address(self, name, address, dnszone):
        dEntry = DomainEntry(self.lobj, dnszone)
        nEntry = dEntry.subentry_with_name(name)
        nEntry.modify_address(name, address)

    def create_domain(self, fqdomain):
        DomainEntry.create_domain(self.lobj, fqdomain)

    def delete_domain(self, fqdomain):
        dEntry = DomainEntry(self.lobj, fqdomain)
        dEntry.delete()

    def delete_dns_file(self):
        LOG.warn("This shouldn't be getting called except during testing.")
        pass


class FakeLdapDNS(LdapDNS):
    """For testing purposes, a DNS driver backed with a fake ldap driver."""
    def __init__(self):
        self.lobj = fakeldap.FakeLDAP()
        attrs = {'objectClass': ['domainrelatedobject', 'dnsdomain',
                                 'domain', 'dcobject', 'top'],
                 'associateddomain': ['root'],
                 'dc': 'root'}
        self.lobj.add_s(flags.FLAGS.ldap_dns_base_dn, create_modlist(attrs))