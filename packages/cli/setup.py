"""Setup for Kestrel CLI package."""

from setuptools import setup, find_packages

setup(
    name="kestrel-cli",
    version="0.1.0",
    description="🦅 Kestrel CLI — Autonomous Agent Platform",
    py_modules=["kestrel"],
    install_requires=[
        "httpx>=0.25.0",
    ],
    entry_points={
        "console_scripts": [
            "kestrel=kestrel:main",
        ],
    },
    python_requires=">=3.11",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Programming Language :: Python :: 3.11",
        "Topic :: Software Development :: Libraries",
    ],
)
