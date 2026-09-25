from setuptools import find_packages, setup

setup(
    name="relief-core",
    version="0.1.0",
    description="企业合规档案基础服务",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
