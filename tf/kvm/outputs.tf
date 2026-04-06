output "floating_ip" {
  description = "Floating IP assigned to node1"
  value       = openstack_networking_floatingip_v2.floating_ip.address
}

output "node_ips" {
  description = "Private network IPs for all nodes"
  value       = { for k, v in var.nodes : k => v }
}
