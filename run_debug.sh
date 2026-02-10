#!/bin/bash
echo "Starting wrapper script" > debug_wrapper.log
pwd >> debug_wrapper.log
which python >> debug_wrapper.log
python debug_image_gen.py >> debug_wrapper.log 2>&1
echo "Finished wrapper script" >> debug_wrapper.log
