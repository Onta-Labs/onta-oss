"""Host-kind classification for NEO4J_URI (health + hosted pin guard)."""

from infona_client.graph.bolt_uri import (
    KIND_HOSTNAME,
    KIND_LOOPBACK,
    KIND_MISSING,
    KIND_PRIVATE_IP,
    KIND_PUBLIC_IP,
    classify_bolt_uri,
)


def test_missing():
    assert classify_bolt_uri(None) == KIND_MISSING
    assert classify_bolt_uri("") == KIND_MISSING
    assert classify_bolt_uri("   ") == KIND_MISSING


def test_loopback():
    assert classify_bolt_uri("bolt://localhost:7687") == KIND_LOOPBACK
    assert classify_bolt_uri("bolt://127.0.0.1:7687") == KIND_LOOPBACK
    assert classify_bolt_uri("bolt://[::1]:7687") == KIND_LOOPBACK


def test_private_ip_is_the_hosted_pin_failure_mode():
    # The 2026-08-24 Explorer hang: Fargate moved Neo4j, API stayed on this IP.
    assert classify_bolt_uri("bolt://10.0.10.176:7687") == KIND_PRIVATE_IP
    assert classify_bolt_uri("bolt://10.0.11.81:7687") == KIND_PRIVATE_IP
    assert classify_bolt_uri("bolt://192.168.1.9:7687") == KIND_PRIVATE_IP
    assert classify_bolt_uri("bolt://172.16.0.4:7687") == KIND_PRIVATE_IP
    assert classify_bolt_uri("neo4j://10.0.10.176:7687") == KIND_PRIVATE_IP


def test_hostname_is_cloud_map():
    assert classify_bolt_uri("bolt://neo4j.infona.local:7687") == KIND_HOSTNAME
    assert classify_bolt_uri("bolt://neo4j:7687") == KIND_HOSTNAME


def test_public_ip():
    assert classify_bolt_uri("bolt://8.8.8.8:7687") == KIND_PUBLIC_IP
