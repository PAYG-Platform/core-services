from setuptools import setup, find_packages

setup(
    name="vpn_shared",
    version="1.0.0",
    description="Shared utility library for PAYG VPN Microservices Platform",
    author="PAYG VPN Team",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "pyjwt>=2.8.0",
        "cryptography>=41.0.0",
        "pydantic>=2.0.0",
        "python-dotenv>=1.0.0",
        "fastapi>=0.100.0"
    ],
)