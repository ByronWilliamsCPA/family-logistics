---
title: "Usage"
schema_type: common
status: published
owner: core-maintainer
purpose: "Usage guide for Family Logistics."
tags:
  - guide
  - usage
---

This guide covers common usage patterns for Family Logistics.

## Installation

### From PyPI

```bash
pip install family-logistics
```

### From Source

```bash
git clone https://github.com/ByronWilliamsCPA/family-logistics
cd family_logistics
uv sync --all-extras
```

## Command Line Interface

### Available Commands

```bash
# Show help
family_logistics --help

# Hello command
family_logistics hello --name "World"

# Show configuration
family_logistics config
```

### Debug Mode

Enable debug logging:

```bash
family_logistics --debug hello --name "Test"
```
## Library Usage

### Basic Import

```python
from family_logistics import __version__

print(f"Version: {__version__}")
```

### Logging

```python
from family_logistics.utils.logging import get_logger, setup_logging

# Setup logging
setup_logging(level="DEBUG", json_logs=False)

# Get a logger
logger = get_logger(__name__)
logger.info("Hello from Family Logistics")
```
