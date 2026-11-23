#version 450
// two textures in one fragment program: the left half from t0, the right from t1
layout(set = 0, binding = 0) uniform sampler2D t0;
layout(set = 0, binding = 1) uniform sampler2D t1;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 c;
void main() { c = uv.x < 0.5 ? texture(t0, uv) : texture(t1, uv); }
