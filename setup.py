from setuptools import setup
from setuptools import find_packages

setup(
    name="cloome",
    version="1.0.0",
    author="Ana Sanchez-Fernandez",
    author_email="sanchez@ml.jku.at",
    package_dir = {
        'cloome': 'src',
    },
    packages=['cloome.clip'],
    include_package_data=True
)
