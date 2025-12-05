from setuptools import setup, find_packages

setup(
    name="rain-on-trump",
    version="0.3.0",
    description="Is It Raining on Trump? backend",
    package_dir={"": "backend"},
    packages=find_packages(where="backend"),
    install_requires=[
        "httpx>=0.23",
        "fastapi>=0.95",
        "uvicorn>=0.22",
        "python-opensky>=1.0.1",
        "python-dateutil>=2.8",
        "geopy>=2.4",
        "beautifulsoup4>=4.12",
        # "icalendar" removed – JSON calendar now
    ],
    extras_require={
        "test": [
            "pytest>=7.0",
            "pytest-asyncio>=0.21",
            "pytest-httpx>=0.23",
        ],
        "lint": [
            "black>=23.0",
            "flake8>=6.0",
        ],
    },
)
