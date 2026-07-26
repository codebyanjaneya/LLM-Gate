# main.rego - OPA security policies for Terraform plan JSON.
#
# Evaluate against the JSON produced by:  terraform show -json <planfile>
# Query:  data.terraform.security
#
# Each rule below is a partial set of human-readable "deny" messages.
# A rule with zero messages = PASS; one or more messages = FAIL.
# policies/run_check.py reads these and gates the pipeline (exit 1 on any fail).

package terraform.security

import rego.v1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Collect every resource from the plan, including nested child modules.
# `walk` visits every node under planned_values; module nodes carry a
# "resources" array, so we pull resources out of each node we find.
all_resources contains resource if {
	walk(input.planned_values, [_, node])
	resource := node.resources[_]
}

# True if a security-group rule allows a given port (or all ports via "-1").
port_in_range(rule, port) if {
	rule.protocol == "-1"
}

port_in_range(rule, port) if {
	rule.from_port <= port
	rule.to_port >= port
}

# True if an instance carries a "Name" tag.
has_name_tag(resource) if {
	resource.values.tags.Name
}

# ---------------------------------------------------------------------------
# Rule 1: no security group open to the world (0.0.0.0/0)
# ---------------------------------------------------------------------------
deny_open_security_group contains msg if {
	some resource in all_resources
	resource.type == "aws_security_group"
	some rule_type in ["ingress", "egress"]
	rule := resource.values[rule_type][_]
	rule.cidr_blocks[_] == "0.0.0.0/0"
	msg := sprintf(
		"Security group '%s' has an %s rule open to the world (0.0.0.0/0)",
		[resource.address, rule_type],
	)
}

# ---------------------------------------------------------------------------
# Rule 2: root volumes must be encrypted
# ---------------------------------------------------------------------------
deny_unencrypted_volume contains msg if {
	some resource in all_resources
	resource.type == "aws_instance"
	rbd := resource.values.root_block_device[_]
	rbd.encrypted == false
	msg := sprintf(
		"Instance '%s' has an unencrypted root_block_device (encrypted = false)",
		[resource.address],
	)
}

# ---------------------------------------------------------------------------
# Rule 3: every instance must carry a "Name" tag
# ---------------------------------------------------------------------------
deny_missing_tags contains msg if {
	some resource in all_resources
	resource.type == "aws_instance"
	not has_name_tag(resource)
	msg := sprintf(
		"Instance '%s' is missing a required 'Name' tag",
		[resource.address],
	)
}

# ---------------------------------------------------------------------------
# Rule 4: no publicly readable/writable S3 buckets
# (covers both inline aws_s3_bucket.acl and the aws_s3_bucket_acl resource)
# ---------------------------------------------------------------------------
deny_public_s3 contains msg if {
	some resource in all_resources
	resource.type in ["aws_s3_bucket", "aws_s3_bucket_acl"]
	resource.values.acl in ["public-read", "public-read-write"]
	msg := sprintf(
		"S3 resource '%s' has a public ACL (%s)",
		[resource.address, resource.values.acl],
	)
}

# ---------------------------------------------------------------------------
# Rule 5: SSH (port 22) must not be open to the world
# ---------------------------------------------------------------------------
deny_ssh_open_to_world contains msg if {
	some resource in all_resources
	resource.type == "aws_security_group"
	some rule_type in ["ingress", "egress"]
	rule := resource.values[rule_type][_]
	rule.cidr_blocks[_] == "0.0.0.0/0"
	port_in_range(rule, 22)
	msg := sprintf(
		"Security group '%s' exposes SSH (port 22) to the world (0.0.0.0/0)",
		[resource.address],
	)
}

# ---------------------------------------------------------------------------
# Convenience aggregate: union of all deny messages (handy for conftest).
# ---------------------------------------------------------------------------
deny contains msg if {
	some ruleset in [
		deny_open_security_group,
		deny_unencrypted_volume,
		deny_missing_tags,
		deny_public_s3,
		deny_ssh_open_to_world,
	]
	some msg in ruleset
}
