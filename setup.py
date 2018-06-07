from setuptools import setup, find_packages

long_description = open('README.md').read()

setup(
    name='infinitytouch',
    version='0.1',
    license='MIT License',
    url='https://github.com/MizterB/InfinityTouch',
    author='MizterB',
    author_email='5458030+MizterB@users.noreply.github.com',
    description='Python-based proxy server with API for controlling Carrier Infinity thermostats',
    long_description=long_description,
    long_description_content_type='text/markdown',
    packages=['infinitytouch'],
    zip_safe=True,
    platforms='any',
    install_requires=list(val.strip() for val in open('requirements.txt')),
    classifiers=[
        'Intended Audience :: Other Audience',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Topic :: Software Development :: Libraries :: Python Modules'
    ]
)