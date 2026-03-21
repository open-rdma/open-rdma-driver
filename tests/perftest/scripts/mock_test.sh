
trap "kill 0" SIGINT

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)


PERF_TEST_PARAMS="--use_hugepages --data_validation_debug -t 128 -x 3"

echo $SCRIPT_DIR

# 编译 rust driver
DTLD_DIR=$SCRIPT_DIR/../../../dtld-ibverbs
cd $DTLD_DIR
cargo build --no-default-features --features=mock


mkdir -p $SCRIPT_DIR/../log/mock
LOG_DIR=$(cd "$SCRIPT_DIR/../log/mock" && pwd)

cd $SCRIPT_DIR/..

echo $(pwd)


RUST_LOG=${RUST_LOG:-info}


sudo env \
	RUST_LOG=${RUST_LOG} \
	LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
	ib_write_bw -d bluerdma0 $PERF_TEST_PARAMS &> $LOG_DIR/server.log &

sudo env \
	RUST_LOG=${RUST_LOG} \
	LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
	ib_write_bw -d bluerdma1 $PERF_TEST_PARAMS 127.0.0.1 &> $LOG_DIR/client.log &



wait

