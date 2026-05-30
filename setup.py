"""MakroGraph Intelligence - Setup."""

from setuptools import setup, find_packages

setup(
    name="makrograph-intelligence",
    version="0.1.0",
    description="Document Acquisition Pipeline - Fetch, Parse, Deduplicate, Normalize, Store",
    author="MakroGraph",
    python_requires=">=3.10",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    entry_points={
        "console_scripts": [
            "makrograph=makrograph.cli:main",
        ],
    },
)
