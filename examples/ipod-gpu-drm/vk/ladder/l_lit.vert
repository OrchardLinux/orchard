#version 450
// a lit mesh as vkmark's vertex/shading scenes have it: position + normal in, per-vertex diffuse lighting out
layout(location = 0) in vec3 pos;
layout(location = 1) in vec3 nrm;
layout(location = 0) out vec4 col;
void main() {
   const vec3 L = normalize(vec3(0.3, -0.5, 0.8));
   float d = max(dot(normalize(nrm), L), 0.0);
   col = vec4(vec3(0.15) + d * vec3(0.8, 0.6, 0.3), 1.0);
   gl_Position = vec4(pos.xy * 0.8, 0.5 - 0.4 * pos.z, 1.0);
}
