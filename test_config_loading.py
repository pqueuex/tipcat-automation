#!/usr/bin/env python3
"""Test config loading and validation."""
import sys
sys.path.insert(0, '/Users/jj/Business/ECommerce/tipcat-pipeline')
from product_automation_script import load_product_config, apply_config

# Test 1: Load phonecases config
print("=" * 70)
print("TEST 1: Load tipcat-phonecases config")
print("=" * 70)
config1 = load_product_config('tipcat-phonecases')
print(f"✓ Name: {config1['name']}")
print(f"✓ Product: {config1['product']['type']}")
print(f"✓ Store: {config1['store']['url']}")
print(f"✓ GCS bucket: {config1['gcs']['bucket']}")
print(f"✓ Printify blueprint: {config1['printify']['blueprint_id']}")
print(f"✓ Variants: {list(config1['printify']['variants'].keys())[:2]}...")
print()

# Test 2: Load mousepads config
print("=" * 70)
print("TEST 2: Load tipcat-mousepads config")
print("=" * 70)
config2 = load_product_config('tipcat-mousepads')
print(f"✓ Name: {config2['name']}")
print(f"✓ Product: {config2['product']['type']}")
print(f"✓ Store: {config2['store']['url']}")
print(f"✓ GCS bucket: {config2['gcs']['bucket']}")
print(f"✓ Printify blueprint: {config2['printify']['blueprint_id']}")
print(f"✓ Variants: {list(config2['printify']['variants'].keys())[:2]}...")
print()

# Test 3: Configs are different
print("=" * 70)
print("TEST 3: Verify configs are distinct")
print("=" * 70)
assert config1['name'] != config2['name'], "Configs should have different names"
assert config1['gcs']['bucket'] != config2['gcs']['bucket'], "Buckets should be different"
print("✓ Configs are properly isolated")
print()

# Test 4: Check required fields
print("=" * 70)
print("TEST 4: Validate config structure")
print("=" * 70)
required = ["name", "product", "store", "gcs", "printify", "gemini", "prompts", "shopify"]
for field in required:
    assert field in config1, f"Missing field: {field}"
    assert field in config2, f"Missing field: {field}"
print(f"✓ All {len(required)} required fields present in both configs")
print()

print("=" * 70)
print("✅ ALL TESTS PASSED")
print("=" * 70)
