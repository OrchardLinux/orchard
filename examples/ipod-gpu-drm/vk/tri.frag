#version 450
// metal-triangle's fragment shader: a constant colour (1, 0.25, 0, 1)
layout(location = 0) out vec4 c;
void main() { c = vec4(1.0, 0.25, 0.0, 1.0); }
