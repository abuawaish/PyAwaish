from setuptools import setup, find_packages

with open('README.rst', encoding='utf-8') as readme_file:
    long_description = readme_file.read()

with open('CHANGELOG.txt', encoding='utf-8') as changelog_file:
    long_description += '\n\n' + changelog_file.read()

setup(
    name="PyAwaish",
    version="1.6.9",
    author="Abu Awaish",
    author_email="abuawaish7@gmail.com",
    description="A Python package for building dynamic MySQL-powered web applications with template support",
    long_description=long_description,
    long_description_content_type="text/x-rst",
    url="https://github.com/abuawaish/PyAwaish",  # point at the repo, not your profile
    project_urls={
        "Source": "https://github.com/abuawaish/PyAwaish",
        "Bug Tracker": "https://github.com/abuawaish/PyAwaish/issues",
    },
    packages=find_packages(include=["PyAwaish", "PyAwaish.*"]),
    include_package_data=True,
    package_data={
        "PyAwaish": [
            "templates/**/*", # Include all template files and subdirectories
            "static/**/*",  # all static assets, not just PNGs
        ],
    },
    zip_safe=False,
    license="MIT",
    license_files=['LICENSE'],
    install_requires=[
        'Flask>=3.1.2,<4',
        'Flask-MySQLdb>=2.0.0,<3',
        'python-dotenv>=1.2.1,<2',
        'mysqlclient>=2.2.8,<3',
    ],
    keywords="web application flask mysql dynamic templates",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Framework :: Flask",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
    ],
    python_requires=">=3.10",  # matches the real floor set by your dependencies
)