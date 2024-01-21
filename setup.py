from setuptools import setup, find_packages
from os.path import join, dirname

setup(
    name='centrifuge-python',
    version='0.3',
    description="Websocket real-time SDK for Centrifugo on top of asyncio library",
    install_requires=[
        "websockets>=11.0.3,<12.0.0",
        "protobuf>=4.23.4,<5.0.0",
    ],
    packages=find_packages(),
    long_description=open(join(dirname(__file__), 'README.md')).read(),
    url='https://github.com/centrifugal/centrifuge-python',
    download_url='https://github.com/centrifugal/centrifuge-python',
    author="Vladimir Denisov",
    license='MIT',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Topic :: Internet :: WWW/HTTP',
        'Topic :: Software Development',
        'Topic :: System :: Networking',
    ]
)
