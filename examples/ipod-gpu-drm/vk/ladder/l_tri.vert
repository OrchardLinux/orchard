#version 450
// the smoke triangle's vertex shader: the position passed through
layout(location = 0) in vec4 pos;
void main() { gl_Position = pos; }
