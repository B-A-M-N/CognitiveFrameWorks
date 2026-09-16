# Release gate

The supported beta artifact is the self-contained installer bundle emitted by
`scripts/install-framework.py`. A release must be cut from an exact tag with
clean CognitiveFrameWorks, CognitiveStateWork, and DigitalPsychology
checkouts. Run `scripts/final-gate.py --tag <tag>` from that checkout.

The gate creates a fresh temporary `HOME` and XDG runtime/state directories.
It never cleans, deletes, or modifies a developer checkout or real home
directory. Wheels are contract-library artifacts only; they are not the
runtime deployment product until a future release defines a self-contained
wheel layout.
