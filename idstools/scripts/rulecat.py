#! /usr/bin/env python
#
# Copyright (c) 2015 Jason Ish
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED ``AS IS'' AND ANY EXPRESS OR IMPLIED
# WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import print_function

import sys
import re
import os.path
import logging
import argparse
import shlex
import time
import hashlib
import fnmatch
import subprocess

try:
    from io import BytesIO
except:
    from StringIO import StringIO as BytesIO

try:
    import progressbar
except:
    progressbar = None

if sys.argv[0] == __file__:
    sys.path.insert(
        0, os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import idstools.rule
import idstools.suricata
import idstools.net
import idstools.util

CONF_SAMPLE = """# rulecat.conf
"""

SAMPLE_ENABLE_CONF = """# enable.conf

# Example of enabling a rule by signature ID (gid is optional).
# 1:2019401
# 2019401

# Example of enabling a rule by regular expression.
# - All regular expression matches are case insensitive.
# re:hearbleed
# re:MS(0[7-9]|10)-\d+
"""

SAMPLE_DISABLE_CONF = """# disable.conf

# Example of disabling a rule by signature ID (gid is optional).
# 1:2019401
# 2019401

# Example of disabling a rule by regular expression.
# - All regular expression matches are case insensitive.
# re:hearbleed
# re:MS(0[7-9]|10)-\d+
"""

SAMPLE_MODIFY_CONF = """# modify.conf

# Format: <sid> "<from>" "<to>"

# Example changing the seconds for rule 2019401 to 3600.
# 2019401 "seconds \d+" "seconds 3600"
"""

SAMPLE_THRESHOLD_IN = """# threshold.in (rulecat)

# This file contains thresholding configurations that will be turned into
# a Suricata compatible threshold.conf file.

# This file can contain standard threshold.conf configurations:
#
# suppress gen_id <gid>, sig_id <sid>
# suppress gen_id <gid>, sig_id <sid>, track <by_src|by_dst>, ip <ip|subnet>
# threshold gen_id 0, sig_id 0, type threshold, track by_src, count 10, seconds 10
# suppress gen_id 1, sig_id 2009557, track by_src, ip 217.110.97.128/25

# Or ones that will be preprocessed...

# Suppress all rules containing "java".
#
# suppress re:java
# suppress re:java, track by_src, ip 217.110.97.128/25

# Threshold all rules containing "java".
#
# threshold re:java, type threshold, track by_dst, count 1, seconds 10

"""

logging.basicConfig(
    level=logging.getLevelName(os.environ.get("RULECAT_LOG_LEVEL", "INFO")),
    format="%(message)s")
logger = logging.getLogger()

ET_PRO_URL = "http://rules.emergingthreatspro.com/%(code)s/suricata%(version)s/etpro.rules.tar.gz"

ET_OPEN_URL = "https://rules.emergingthreats.net/open/suricata%(version)s/emerging.rules.tar.gz"

class IdRuleMatcher(object):
    """Matcher object to match an idstools rule object by its signature
    ID."""

    def __init__(self, generatorId, signatureId):
        self.generatorId = generatorId
        self.signatureId = signatureId

    def match(self, rule):
        return self.generatorId == rule.gid and self.signatureId == rule.sid

    @classmethod
    def parse(cls, match):
        try:
            signatureId = int(match)
            return cls(1, signatureId)
        except:
            pass
        try:
            generatorString, signatureString = match.split(":")
            generatorId = int(generatorString)
            signatureId = int(signatureString)
            return cls(generatorId, signatureId)
        except:
            pass
        return None

class GroupMatcher(object):
    """Matcher object to match an idstools rule object by its group (ie:
    filename)."""

    def __init__(self, pattern):
        self.pattern = pattern

    def match(self, rule):
        if hasattr(rule, "group") and rule.group is not None:
            return fnmatch.fnmatch(rule.group, self.pattern)
        return False

    @classmethod
    def parse(cls, match):
        if match.startswith("group:"):
            try:
                group = match.split(":", 1)[1]
                return cls(group.strip())
            except:
                pass
        return None

class ReRuleMatcher(object):
    """Matcher object to match an idstools rule object by regular
    expression."""

    def __init__(self, pattern):
        self.pattern = pattern

    def match(self, rule):
        if self.pattern.search(rule.raw):
            return True
        return False

    @classmethod
    def parse(cls, buf):
        if buf.startswith("re:"):
            try:
                patternstr = buf.split(":", 1)[1].strip()
                logger.debug(
                    "Compiling regular expression match: %s" % (patternstr))
                pattern = re.compile(patternstr, re.I)
                return cls(pattern)
            except:
                pass
        return None

class ModifyRuleFilter(object):
    """Filter to modify an idstools rule object.

    Important note: This filter does not modify the rule inplace, but
    instead returns a new rule object with the modification.
    """

    def __init__(self, matcher, pattern, repl):
        self.matcher = matcher
        self.pattern = pattern
        self.repl = repl

    def match(self, rule):
        return self.matcher.match(rule)

    def filter(self, rule):
        return idstools.rule.parse(
            self.pattern.sub(self.repl, str(rule)), rule.group)

    @classmethod
    def parse(cls, buf):
        tokens = shlex.split(buf)
        if len(tokens) != 3:
            raise Exception("Bad number of arguments.")
        matcher = parse_rule_match(tokens[0])
        if not matcher:
            raise Exception("Bad match string: %s" % (tokens[0]))
        pattern = re.compile(tokens[1])
        return cls(matcher, pattern, tokens[2])

class Fetch(object):

    def __init__(self, args):
        self.args = args

    def get_rule_url(self):
        if self.args.url:
            return self.args.url
        mappings = {}
        if self.args.etpro:
            template_url = ET_PRO_URL
            mappings["code"] = self.args.etpro
        else:
            template_url = ET_OPEN_URL
        suricata_version = idstools.suricata.get_version(self.args.suricata)
        if not suricata_version or not suricata_version.short:
            mappings["version"] = ""
        else:
            mappings["version"] = "-" + suricata_version.short
        return template_url % mappings

    def check_checksum(self, tmp_filename, url):
        try:
            checksum_url = url + ".md5"
            local_checksum = hashlib.md5(open(tmp_filename).read()).hexdigest()
            remote_checksum_buf = BytesIO()
            logger.info("Fetching %s." % (checksum_url))
            remote_checksum = idstools.net.get(
                checksum_url, remote_checksum_buf)
            logger.debug("Local checksum=|%s|; remote checksum=|%s|" % (
                local_checksum.strip(), remote_checksum_buf.getvalue().strip()))
            if local_checksum.strip() == remote_checksum_buf.getvalue().strip():
                os.utime(tmp_filename, None)
                return True
        except Exception as err:
            logger.error("Failed to check remote checksum: %s" % err)
        return False

    def progress_hook(self, content_length, bytes_read):
        percent = int((bytes_read / float(content_length)) * 100)
        buf = " %3d%% - %-30s" % (
            percent, "%d/%d" % (bytes_read, content_length))
        sys.stdout.write(buf)
        sys.stdout.flush()
        sys.stdout.write("\b" * 38)
        if bytes_read and bytes_read >= content_length:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def run(self):
        url = self.get_rule_url()
        tmp_filename = os.path.join(self.args.temp, os.path.basename(url))
        if not self.args.force and os.path.exists(tmp_filename):
            if time.time() - os.stat(tmp_filename).st_mtime < (60 * 15):
                logger.info(
                    "Last download less than 15 minutes ago. Not fetching.")
                return self.files_as_dict()
            if self.check_checksum(tmp_filename, url):
                logger.info("Remote checksum has not changed. Not fetching.")
                return self.files_as_dict()
        if not os.path.exists(self.args.temp):
            os.makedirs(self.args.temp)
        logger.info("Fetching %s." % (url))
        idstools.net.get(
            url, open(tmp_filename, "wb"), progress_hook=self.progress_hook)
        logger.info("Done.")
        return self.files_as_dict()

    def basename(self):
        return os.path.basename(self.get_rule_url())

    def files_as_dict(self):
        files = idstools.util.archive_to_dict(
            os.path.join(self.args.temp, self.basename()))

        # Erase path information.
        for key in files.keys():
            files[os.path.basename(key)] = files.pop(key)

        return files

def parse_rule_match(match):
    matcher = IdRuleMatcher.parse(match)
    if matcher:
        return matcher
    matcher = ReRuleMatcher.parse(match)
    if matcher:
        return matcher
    matcher = GroupMatcher.parse(match)
    if matcher:
        return matcher
    return None

def load_filters(filename):

    filters = []

    with open(filename) as fileobj:
        for line in fileobj:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            filter = ModifyRuleFilter.parse(line)
            if filter:
                filters.append(filter)

    return filters

def load_matchers(filename):

    matchers = []

    with open(filename) as fileobj:
        for line in fileobj:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            matcher = parse_rule_match(line)
            if not matcher:
                logger.warn("Failed to parse: \"%s\"" % (line))
            else:
                matchers.append(matcher)

    return matchers

def load_local_files(local, files):
    """Load local files into the files dict."""
    if os.path.isdir(local):
        for dirpath, dirnames, filenames in os.walk(local):
            for filename in filenames:
                if filename.endswith(".rules"):
                    path = os.path.join(local, filename)
                    if filename in files:
                        logger.warn(
                            "Local file %s overrides existing file of "
                            "same name." % (path))
                    files[filename] = open(path).read()
    else:
        filename = os.path.basename(local)
        if filename in files:
            logger.warn(
                "Local file %s overrides existing file of same name." % (
                    local))
        files[filename] = open(local).read()

def write_to_directory(directory, files, rulemap):
    logger.info("Writing rule files to %s." % (directory))
    for filename in sorted(files):
        outpath = os.path.join(
            directory, os.path.basename(filename))
        logger.debug("Writing %s." % outpath)
        if not filename.endswith(".rules"):
            open(outpath, "wb").write(files[filename])
        else:
            content = []
            for line in BytesIO(files[filename]):
                rule = idstools.rule.parse(line)
                if not rule:
                    content.append(line.strip())
                else:
                    content.append(str(rulemap[rule.id]))
            open(outpath, "wb").write("\n".join(content))

def build_report(prev_rulemap, rulemap):
    """Build a report of changes between 2 rulemaps.

    Returns a dict with the following keys that each contain a list of
    rules.
    - added
    - removed
    - modified
    """
    report = {
        "added": [],
        "removed": [],
        "modified": []
    }

    for rule in rulemap.itervalues():
        if not rule.id in prev_rulemap:
            report["added"].append(rule)
        elif str(rule) != str(prev_rulemap[rule.id]):
            report["modified"].append(rule)
    for rule in prev_rulemap.itervalues():
        if not rule.id in rulemap:
            report["removed"].append(rule)

    return report

def write_merged(filename, rulemap):

    prev_rulemap = {}
    if os.path.exists(filename):
        prev_rulemap = build_rule_map(
            idstools.rule.parse_fileobj(open(filename)))
    report = build_report(prev_rulemap, rulemap)

    logger.info("Writing %s: added: %d; removed %d; modified: %d" % (
        filename, len(report["added"]), len(report["removed"]),
        len(report["modified"])))
    with open(filename, "w") as fileobj:
        for rule in rulemap:
            print(str(rulemap[rule]), file=fileobj)

def write_yaml_fragment(filename, files):
    logger.info(
        "Writing YAML configuration fragment: %s" % (filename))
    with open(filename, "w") as fileobj:
        print("%YAML 1.1", file=fileobj)
        print("---", file=fileobj)
        print("rule-files:", file=fileobj)
        for fn in sorted(files):
            if fn.endswith(".rules"):
                print("  - %s" % os.path.basename(fn), file=fileobj)

def write_sid_msg_map(filename, rulemap, version=1):
    logger.info("Writing %s." % (filename))
    with open(filename, "w") as fileobj:
        for rule in rulemap.itervalues():
            if version == 2:
                print(idstools.rule.format_sidmsgmap_v2(rule), file=fileobj)
            else:
                print(idstools.rule.format_sidmsgmap(rule), file=fileobj)

def build_rule_map(rules):
    """Turn a list of rules into a mapping of rules.

    In case of gid:sid conflict, the rule with the higher revision
    number will be used.
    """
    rulemap = {}

    for rule in rules:
        if rule.id not in rulemap:
            rulemap[rule.id] = rule
        else:
            if rule["rev"] > rulemap[rule.id]["rev"]:
                rulemap[rule.id] = rule

    return rulemap

def dump_sample_configs():

    def write_file(filename, content):
        logger.info("Writing %s." % (filename))
        open(filename, "w").write(content)

    files = {
        "enable.conf": SAMPLE_ENABLE_CONF,
        "disable.conf": SAMPLE_DISABLE_CONF,
        "modify.conf": SAMPLE_MODIFY_CONF,
        "threshold.in": SAMPLE_THRESHOLD_IN,
    }

    for filename in files:
        if os.path.exists(filename):
            logger.info("File already exists, not dumping %s." % (filename))
        else:
            write_file(filename, files[filename])

def resolve_flowbits(rulemap, disabled_rules):
    flowbit_resolver = idstools.rule.FlowbitResolver()
    flowbit_enabled = set()
    while True:
        flowbits = flowbit_resolver.get_required_flowbits(rulemap)
        logger.debug("Found %d required flowbits.", len(flowbits))
        required_rules = flowbit_resolver.get_required_rules(rulemap, flowbits)
        logger.debug(
            "Found %d rules to enable to fullfull flowbit requirements",
            len(required_rules))
        if not required_rules:
            logger.debug("All required rules enabled.")
            break
        for rule in required_rules:
            if not rule.enabled and rule in disabled_rules:
                logger.debug(
                    "Enabling previously disabled rule for flowbits: %s" % (
                        rule.brief()))
            rule.enabled = True
            flowbit_enabled.add(rule)
    logger.info("Enabled %d rules for flowbit dependencies." % (
        len(flowbit_enabled)))

class ThresholdProcessor:

    patterns = [
        re.compile("\s+(re:\"(.*)\")"),
        re.compile("\s+(re:(.*?)),.*"),
        re.compile("\s+(re:(.*))"),
    ]

    def extract_regex(self, buf):
        for pattern in self.patterns:
            m = pattern.search(buf)
            if m:
                return m.group(2)

    def extract_pattern(self, buf):
        regex = self.extract_regex(buf)
        if regex:
            return re.compile(regex, re.I)

    def replace(self, threshold, rule):
        for pattern in self.patterns:
            m = pattern.search(threshold)
            if m:
                return threshold.replace(
                    m.group(1), "gen_id %d, sig_id %d" % (rule.gid, rule.sid))
        return thresold

    def process(self, filein, fileout, rulemap):
        count = 0
        for line in filein:
            line = line.rstrip()
            if not line or line.startswith("#"):
                print(line, file=fileout)
                continue
            pattern = self.extract_pattern(line)
            if not pattern:
                print(line, file=fileout)
            else:
                for rule in rulemap.values():
                    if rule.enabled:
                        if pattern.search(str(rule)):
                            count += 1
                            print("# %s" % (rule.brief()), file=fileout)
                            print(self.replace(line, rule), file=fileout)
                            print("", file=fileout)
        logger.info("Generated %d thresholds to %s." % (count, fileout.name))

class HashTracker:
    """Used to check if files are modified.

    Usage: Add files with add(filename) prior to modification. Test
    with any_modified() which will return True if any of the checksums
    have been modified.

    """

    def __init__(self):
        self.hashes = {}

    def add(self, filename):
        checksum = self.get_md5(filename)
        logger.debug("Recording file %s with hash '%s'.", filename, checksum)
        self.hashes[filename] = checksum

    def get_md5(self, filename):
        if not os.path.exists(filename):
            return ""
        elif os.path.isdir(filename):
            return self.get_md5_for_directory(filename)
        else:
            return hashlib.md5(open(filename, "rb").read()).hexdigest()

    def get_md5_for_directory(self, directory):
        checksum = hashlib.md5()
        for filename in sorted(os.listdir(directory)):
            path = os.path.join(directory, filename)
            checksum.update(open(path, "rb").read())
        return checksum.hexdigest()

    def any_modified(self):
        for filename in self.hashes:
            if self.get_md5(filename) != self.hashes[filename]:
                return True
        return False

def main():

    conf_filenames = [arg for arg in sys.argv if arg.startswith("@")]
    if not conf_filenames:
        if os.path.exists("./rulecat.conf"):
            logger.info("Loading ./rulecat.conf.")
            sys.argv.insert(1, "@./rulecat.conf")

    suricata_path = idstools.suricata.get_path()

    parser = argparse.ArgumentParser(fromfile_prefix_chars="@")
    parser.add_argument("-v", "--verbose", action="store_true", default=False,
                        help="Be more verbose")
    parser.add_argument("-t", "--temp", default="/var/tmp/idstools-rulecat",
                        metavar="<directory>",
                        help="Temporary work directory")
    parser.add_argument("--suricata", default=suricata_path,
                        metavar="<path>",
                        help="Path to Suricata program (default: %s)" %
                        suricata_path)
    parser.add_argument("-f", "--force", action="store_true", default=False,
                        help="Force operations that might otherwise be skipped")
    parser.add_argument("--rules-dir", metavar="<directory>",
                        help="Output rules directory.")
    parser.add_argument("--merged", default=None, metavar="<filename>",
                        help="Output merged rules file")
    parser.add_argument("--yaml-fragment", metavar="<filename>",
                        help="Output YAML fragment for rule inclusion")
    parser.add_argument("--url", metavar="<url>",
                        help="URL to use instead of auto-generating one")
    parser.add_argument("--local", metavar="<filename>",
                        help="Local rule files or directories")
    parser.add_argument("--sid-msg-map", metavar="<filename>",
                        help="Generate a sid-msg.map file")
    parser.add_argument("--sid-msg-map-2", metavar="<filename>",
                        help="Generate a v2 sid-msg.map file")
    parser.add_argument("--disable", metavar="<filename>",
                        help="Filename of disable rule configuration")
    parser.add_argument("--enable", metavar="<filename>",
                        help="Filename of enable rule configuration")
    parser.add_argument("--modify", metavar="<filename>",
                        help="Filename of rule modification configuration")
    parser.add_argument("--threshold-in", metavar="<filename>",
                        help="Filename of rule thresholding configuration")
    parser.add_argument("--threshold-out", metavar="<filename>",
                        help="Output of processed threshold configuration")
    parser.add_argument("--dump-sample-configs", action="store_true",
                        default=False,
                        help="Dump sample config files to current directory")
    parser.add_argument("--etpro", metavar="<etpro-code>",
                        help="Use ET-Pro rules with provided ET-Pro code")
    parser.add_argument("-q", "--quiet", action="store_true", default=False,
                       help="Be quiet, warning and error messages only")
    parser.add_argument("--post-hook", metavar="<command>",
                        help="Command to run after update if modified")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)
    if args.quiet:
        logger.setLevel(logging.WARNING)

    if args.dump_sample_configs:
        return dump_sample_configs()

    hash_tracker = HashTracker()

    disable_matchers = []
    enable_matchers = []
    modify_filters = []

    if args.disable and os.path.exists(args.disable):
        disable_matchers += load_matchers(args.disable)
    if args.enable and os.path.exists(args.enable):
        enable_matchers += load_matchers(args.enable)
    if args.modify and os.path.exists(args.modify):
        modify_filters += load_filters(args.modify)

    files = Fetch(args).run()

    if args.local:
        load_local_files(args.local, files)

    rules = []
    for filename in files:
        logger.debug("Parsing %s." % (filename))
        rules += idstools.rule.parse_fileobj(
            BytesIO(files[filename]), filename)

    rulemap = build_rule_map(rules)
    logger.info("Loaded %d rules." % (len(rules)))

    # Counts of user enabled and modified rules.
    enable_count = 0
    modify_count = 0

    # List of rules disabled by user. Used for counting, and to log
    # rules that are re-enabled to meeto flowbit requirements.
    disabled_rules = []

    for key, rule in rulemap.items():

        for matcher in disable_matchers:
            if rule.enabled and matcher.match(rule):
                logger.debug("Disabling: %s" % (rule.brief()))
                rule.enabled = False
                disabled_rules.append(rule)

        for matcher in enable_matchers:
            if not rule.enabled and matcher.match(rule):
                logger.debug("Enabling: %s" % (rule.brief()))
                rule.enabled = True
                enable_count += 1

        # Unlike enable and disable, modify returns a new instance of
        # the rule.
        for filter in modify_filters:
            if filter.match(rule):
                rulemap[rule.id] = filter.filter(rule)
                modify_count += 1

    logger.info("Disabled %d rules." % (len(disabled_rules)))
    logger.info("Enabled %d rules." % (enable_count))
    logger.info("Modified %d rules." % (modify_count))

    # Fixup flowbits.
    resolve_flowbits(rulemap, disabled_rules)

    if args.rules_dir:
        if not os.path.exists(args.rules_dir):
            logger.info("Making directory %s.", args.rules_dir)
            os.makedirs(args.rules_dir)
        hash_tracker.add(args.rules_dir)
        write_to_directory(args.rules_dir, files, rulemap)

    if args.merged:
        hash_tracker.add(args.merged)
        write_merged(args.merged, rulemap)

    if args.yaml_fragment:
        hash_tracker.add(args.yaml_fragment)
        write_yaml_fragment(args.yaml_fragment, files)

    if args.sid_msg_map:
        write_sid_msg_map(args.sid_msg_map, rulemap, version=1)
    if args.sid_msg_map_2:
        write_sid_msg_map(args.sid_msg_map_2, rulemap, version=2)

    if args.threshold_in and args.threshold_out:
        hash_tracker.add(args.threshold_out)
        threshold_processor = ThresholdProcessor()
        threshold_processor.process(
            open(args.threshold_in), open(args.threshold_out, "w"), rulemap)

    if args.post_hook:
        if args.force:
            logger.info("Running post-hook as force is set.")
        elif hash_tracker.any_modified():
            logger.info("Running post-hook as output has been modified.")
        else:
            logger.info("No modification to output files, "
                        "post-hook will not be run.")
        if args.force or hash_tracker.any_modified():
            logger.info("Running %s." % (args.post_hook))
            subprocess.Popen(args.post_hook, shell=True).wait()

    logger.info("Done.")

    return 0

if __name__ == "__main__":
    sys.exit(main())
