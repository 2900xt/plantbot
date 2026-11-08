# LeafLink

An operate-mode application for field monitoring: camera images, paired VOC measurements and GPS positions feed a persistent anomaly map and image library.

The computer-vision product target is binary: infected/diseased versus healthy. Plant species identification is not required. The supplied model's class names are internal inputs to the binary mapping; a species mismatch alone is not an infection-detection error. Validate binary outcomes and rejection coverage separately from processing capacity.

Images record receipt/order, capture time and location, pass the supplied SegFormer-B2 ground filter and exact v2 splitter, then classify each leaf candidate with the supplied epoch-8 CUDA FP16 model. The user-confirmed Detection % rule is the fraction of candidates predicted infected. New photo policies are snapshotted; old count-based snapshots remain valid. One qualifying photo creates one CV event and a bounded, expiring cache copy. Candidate counts do not guarantee independent leaves; model scores are not calibrated diagnoses.

VOC uses fixed references per device, positive top and differential rises, and a configurable consecutive hold. It emits one anomaly per sustained episode, with persisted device state. VOC anomalies do not establish disease. Missing positions remain unmapped; a displayed path joins recorded same-device GPS fixes only.

The user's paper sketch and SketchPad reference replace the original green dashboard identity. Use the approved public TypeUI Doodle guide, cream paper, ink outlines, peach controls and self-hosted handwritten type. Primary tabs are Field Overview and Images. Overview contains the path/map, detection slider and separate collapsible VOC/CV panels; recent images and full-photo viewing belong to Images. Sensor ingestion and advanced event rules remain accessible via Field setup. Double-click map markers to open linked photos, with keyboard/button alternatives.

The browser frontend is compiled; Node/Python/CUDA server and SQLite provide storage/processing. Empty, disconnected, running, blurry, failed and missing-CUDA states remain explicit. Simulations are marked. No production test observations are installed. Model inference still requires a CUDA-capable server.
