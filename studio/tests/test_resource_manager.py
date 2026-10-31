"""Tests for resource management functionality."""

from studio.comfy_studio.resource_manager import ResourceManager


def test_gpu_allocation():
    """Test GPU allocation."""
    manager = ResourceManager()
    assert manager.allocate_gpu() is True
    assert manager.get_available_gpus() == 0


def test_cpu_allocation():
    """Test CPU allocation."""
    manager = ResourceManager()
    assert manager.allocate_cpu(2) is True
    assert manager.get_available_cpu() == 2


def test_memory_allocation():
    """Test memory allocation."""
    manager = ResourceManager()
    assert manager.allocate_memory(4096) is True
    assert manager.get_available_memory() == 4096


def test_concurrent_tasks():
    """Test concurrent task handling."""
    manager = ResourceManager()
    assert manager.allocate_gpu() is True
    assert manager.allocate_cpu(2) is True
    assert manager.allocate_memory(4096) is True
    assert manager.get_available_gpus() == 0