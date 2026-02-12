from setuptools import setup, find_namespace_packages

with open('README.rst', encoding='utf-8') as readme_file:
    long_description = readme_file.read()

with open('CHANGELOG.txt', encoding='utf-8') as changelog_file:
    long_description += '\n\n' + changelog_file.read()

setup(
    name="PyAwaish",
    version="1.6.8",
    author="Abu Awaish",
    author_email="abuawaish7@gmail.com",
    description="A Python package for building dynamic MySQL-powered web applications with template support",
    long_description=long_description,
    long_description_content_type="text/x-rst",  # Use RST for README and changelog
    url="https://github.com/abuawaish",
    packages=find_namespace_packages(include=["PyAwaish", "PyAwaish.*"]),
    include_package_data=True,  # Include non-code files specified in MANIFEST.in
    package_data={
        "PyAwaish": [
            "templates/**/*",  # Include all template files and subdirectories
            "static/**/*"  # Include all PNG images in the static folder
        ],
    },
    license="MIT",
    license_files=['LICENSE.txt'],  # Explicitly include the license file
    install_requires=[
        'Flask==3.1.2',
        'Flask-MySQLdb==2.0.0',
        'python-dotenv==1.2.1',
        'mysqlclient==2.2.8',
    ],
    keywords="web application flask mysql dynamic templates",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.6",  # Specify the minimum Python version
)
