#version 450
// a uniform-buffer colour
layout(set = 0, binding = 0) uniform U { vec4 color; } u;
layout(location = 0) out vec4 c;
void main() { c = u.color; }
