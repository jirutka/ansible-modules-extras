#!/usr/bin/python
# -*- coding: utf-8 -*-

# (c) 2015, Jakub Jirutka <jakub@jirutka.cz>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

DOCUMENTATION = '''
---
module: mvn_get
author: Jakub Jirutka
version_added: "1.9"
short_description: Downloads artifacts from Maven repository.
description:
  - Downloads artifacts from Maven repository. Resolves version of the artifact, but does I(not)
    resolve its dependencies. It's intended just for fetching single artifact from repository,
    nothing more.
  - The remote server I(must) have direct access to the remote Maven repository.
  - This module requires Python 2.5+.
options:
  name:
    description:
      - Maven coordinates of the artifact to download (see
        U(http://maven.apache.org/pom.html#Maven_Coordinates) for syntax).
      - This option is mutually exclusive with C(group_id) and C(artifact_id).
        C(version), C(classifier) or C(extension) are used only when they are not specified in
        C(name).
    aliases: [ artifact ]
  group_id:
    description:
      - GroupId of the artifact to download.
  artifact_id:
    description:
      - ArtifactId of the artifact to download.
  version:
    description:
      - Version of the artifact to download. When not provided, the newest
        version in repository is resolved.
  classifier:
    description:
      - Classifier of the artifact to download.
  extension:
    description:
      - File extension of the artifact to download.
    default: jar
  repo_url:
    description:
      - URL of the Maven repository to download artifact from.
    default: http://repo1.maven.org/maven2
  repo_username:
    description:
      - The username for use in HTTP Basic authentication.
    aliases: [ username ]
  repo_password:
    description:
      - The password for use in HTTP Basic authentication.
      - If the C(url_username) parameter is not specified, the C(url_password) parameter will not
        be used.
    aliases: [ password ]
    default: ''
  dest:
    description:
      - Absolute path of where to download the file to.
      - If C(dest) is a directory, filename will be derived from artifactId,
      classifier and extension of the artifact.
    required: true
  state:
    choices: [ present, absent ]
    default: present
  others:
    description:
      - all arguments accepted by the M(file) module also work here
requirements: [ hashlib, urllib2, xml.etree ]
'''

EXAMPLES = '''
- name: download artifact from Maven Central
  mvn_get: >
    name=org.apache.maven:maven:3.2.1
    dest=/tmp

- name: download artifact from Maven Central
  mvn_get: >
    group_id=org.apache.maven
    artifact_id=maven
    version=3.2.1
    dest=/tmp/maven.jar

- name: download the newest version of artifact from Maven Central
  mvn_get: >
    name=org.apache.maven:maven
    dest=/tmp

- name: download SNAPSHOT version from a private Maven repository
  mvn_get: >
    name=org.apache.maven:maven:3.2.2-SNAPSHOT
    repo_url=https://maven.example.org/content/repositories/snapshots
    repo_username=flynn
    repo_password=top-secret
    dest=/tmp
'''

import hashlib
import sys
import xml.etree.ElementTree as ET
from base64 import b64encode
from os import path
from urllib2 import Request, urlopen, URLError, HTTPError


class Artifact(object):

    def __init__(self, group_id, artifact_id, version, classifier=None, extension=None):
        if not group_id or not artifact_id:
            raise ArtifactError(None, 'artifact_id and group_id must be provided')
        self.group_id = group_id
        self.artifact_id = artifact_id
        self.version = version
        self.classifier = classifier
        self.extension = extension

    def is_snapshot(self):
        return self.version.endswith('SNAPSHOT')

    def path(self, with_version=True):
        base = self.group_id.replace('.', '/') + '/' + self.artifact_id
        return base + ('/' + self.version if with_version else '')

    def filename(self):
        s = self.artifact_id
        if self.classifier:
            s += '-' + self.classifier
        return s + '.' + self.extension

    def __str__(self):
        a = [self.group_id, self.artifact_id]
        if self.extension != 'jar' or self.classifier:
            a.append(self.extension)
        if self.classifier:
            a.append(self.classifier)
        if self.version:
            a.append(self.version)
        return ':'.join(a)

    @staticmethod
    def parse(input):
        parts = input.split(':')
        if len(parts) < 2:
            raise ArtifactError(None, "invalid Maven coordinates: %s" % input)
        g, a, v, c, t = parts[0], parts[1], None, None, None
        if len(parts) >= 3:
            v = parts[-1]
        if len(parts) >= 4:
            t = parts[2]
        if len(parts) >= 5:
            c = parts[3]
        return Artifact(g, a, v, c, t)


class MavenDownloader:

    def __init__(self, base, username=None, password=None):
        if base.endswith('/'):
            base = base[0:-1]
        self.base = base
        self.username = username
        self.password = password
        self.user_agent = 'Ansible'

    def download(self, artifact, dest, check_mode):
        if path.isdir(dest):
            dest = path.join(dest, artifact.filename())
        elif not path.isdir(path.dirname(dest)):
            raise Error("Destination directory %s does not exist" % dest)

        if not artifact.version:
            artifact.version = self._find_latest_version_available(artifact)

        url = self.find_uri_for_artifact(artifact)
        info = dict(url=url, path=dest, name=str(artifact), **artifact.__dict__)

        if not self._is_same_md5(dest, url + '.md5'):
            response = self._request(url, "Failed to download artifact %s" % str(artifact), lambda r: r)
            if response:
                f = open(dest, 'w')
                try:
                    if check_mode:
                        response.read()
                    else:
                        f.write(response.read())
                finally:
                    f.close()
                return dict(changed=True, **info)
        return dict(changed=False, **info)

    def find_uri_for_artifact(self, artifact):
        if artifact.is_snapshot():
            path = "/%s/maven-metadata.xml" % (artifact.path())
            xml = self._request(self.base + path, 'Failed to download maven-metadata.xml', lambda r: ET.parse(r))
            if not xml:
                raise ArtifactError(artifact, "Metadata for artifact %(artifact)s not found")
            elems = xml.findall('./versioning/snapshotVersions/snapshotVersion')
            return self._find_matching_artifact(elems, artifact)
        else:
            return self._uri_for_artifact(artifact)

    def _find_latest_version_available(self, artifact):
        path = "/%s/maven-metadata.xml" % artifact.path(False)
        xml = self._request(self.base + path, 'Failed to download maven-metadata.xml', lambda r: ET.parse(r))
        if not xml:
            raise ArtifactError(artifact, "Metadata for artifact %(artifact)s not found")
        return xml.findall('./versioning/versions/version')[-1].text

    def _find_matching_artifact(self, elems, artifact):
        filtered = [e for e in elems if e.findtext('extension') == artifact.extension]
        if artifact.classifier:
            filtered = [e for e in filtered if e.findtext('classifier') == artifact.classifier]

        if not len(filtered):
            raise ArtifactError(artifact, "Artifact %(artifact)s not found")
        elem = filtered[0]
        version = elem.findtext('value')

        return self._uri_for_artifact(artifact, version)

    def _uri_for_artifact(self, artifact, version=None):
        if artifact.is_snapshot() and not version:
            raise ArtifactError(artifact, 'Expected unique version for snapshot artifact')
        elif not artifact.is_snapshot():
            version = artifact.version

        uri = "%s/%s/%s-%s" % (self.base, artifact.path(), artifact.artifact_id, version)
        if artifact.classifier:
            uri += '-' + artifact.classifier
        return uri + '.' + artifact.extension

    def _request(self, url, failmsg, f):
        headers = {'User-Agent': self.user_agent}
        if self.username and self.password:
            credentials = b64encode(self.username + ':' + self.password)
            headers['Authorization'] = "Basic %s" % credentials

        req = Request(url, None, headers)
        try:
            response = urlopen(req)
        except (HTTPError, URLError), e:
            raise DownloaderError(url, failmsg, e)
        else:
            return f(response)

    def _is_same_md5(self, file, remote_md5):
        if not path.exists(file):
            return False
        else:
            local_md5 = self._local_md5(file)
            remote = self._request(remote_md5, 'Failed to download MD5', lambda r: r.read())
            return local_md5 == remote

    def _local_md5(self, file):
        md5 = hashlib.md5()
        f = open(file, 'rb')
        try:
            for chunk in iter(lambda: f.read(8192), ''):
                md5.update(chunk)
        finally:
            f.close()
        return md5.hexdigest()


class Error(Exception):

    def __init__(self, message=None):
        self.message

    def __str__(self):
        return self.message


class ArtifactError(Error):

    def __init__(self, artifact, message=None):
        self.artifact = artifact
        self.message = message.format({'artifact': artifact})


class DownloaderError(Error):

    def __init__(self, url, message, cause=None):
        self.url = url
        self.message = message
        self.cause = cause

    def __str__(self):
        return "%s; %s" % (self.message, str(self.cause))


def main():
    module = AnsibleModule(
        argument_spec={
            'name':          {'aliases': ['artifact']},
            'group_id':      {},
            'artifact_id':   {},
            'version':       {},
            'classifier':    {},
            'extension':     {'default': 'jar'},
            'repo_url':      {'aliases': ['repo_uri'], 'default': 'http://repo1.maven.org/maven2'},
            'repo_username': {'aliases': ['username']},
            'repo_password': {'aliases': ['password'], 'default': ''},
            'dest':          {'required': True},
            'state':         {'choices': ['present'], 'default': 'present'}
        },
        required_one_of=[['name', 'group_id']],
        mutually_exclusive=[['name', arg] for arg in ('group_id', 'artifact_id')],
        required_together=[['group_id', 'artifact_id']],
        add_file_common_args=True,
        supports_check_mode=True
    )

    if sys.version_info < (2, 5, 0):
        module.fail_json(msg='This module requires Python 2.5+')

    # Create type object as namespace for module params
    p = type('Params', (), module.params)

    try:
        if p.name:
            artifact = Artifact.parse(p.name)
            artifact.version = artifact.version or p.version
            artifact.classifier = artifact.classifier or p.classifier
            artifact.extension = artifact.extension or p.extension
        else:
            artifact = Artifact(p.group_id, p.artifact_id, p.version,
                                p.classifier, p.extension)

        dw = MavenDownloader(p.repo_url, p.repo_username, p.repo_password)
        dest = path.expanduser(p.dest)

        result = dw.download(artifact, dest, module.check_mode)
        module.exit_json(**result)

    except ArtifactError, e:
        module.fail_json(msg=str(e), name=str(e.artifact))
    except DownloaderError, e:
        module.fail_json(msg=str(e), url=str(e.url))
    except Error, e:
        module.fail_json(msg=str(e))

# import module snippets
from ansible.module_utils.basic import *
main()
