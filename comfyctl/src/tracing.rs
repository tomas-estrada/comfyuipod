use opentelemetry::{global, trace::Tracer};
use opentelemetry_jaeger::Propagator;
use opentelemetry_sdk::propagation::TraceContextPropagator;

pub fn setup_tracing() {
    global::set_text_map_propagator(TraceContextPropagator::new());
    let tracer = opentelemetry_jaeger::new_agent_pipeline()
        .with_service_name("comfyctl")
        .install_simple()
        .unwrap();
    global::set_tracer_provider(tracer);
}

pub fn get_tracer() -> impl Tracer {
    global::tracer("comfyctl")
}