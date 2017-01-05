from setuptools import setup, find_packages
from os.path import join, dirname

setup(
    name='centrifuge-python',
    version='0.1',
    install_requires=["websockets>=3.2"],
    packages=find_packages(),
    long_description=open(join(dirname(__file__), 'README.md')).read(),
)
