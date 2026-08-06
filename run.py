#!/usr/bin/env python3
"""根级启动器。用法: python run.py [--once|--status|--help]"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.run import main
main()
