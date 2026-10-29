package require ::quartus::project
package require ::quartus::flow

if {$argc != 5} {
    puts stderr "usage: quartus_synth.tcl <bundle-dir> <work-dir> <top> <family> <device>"
    exit 2
}

set bundle_dir [file normalize [lindex $argv 0]]
set work_dir [file normalize [lindex $argv 1]]
set top [lindex $argv 2]
set family [lindex $argv 3]
set device [lindex $argv 4]
set qip_file [file join $bundle_dir dma_engine.qip]

if {![file isfile $qip_file]} {
    puts stderr "Quartus QIP does not exist: $qip_file"
    exit 2
}

file mkdir $work_dir
set original_dir [pwd]
set project_name dma_engine_quartus_check
set project_opened 0
set failed [catch {
    cd $work_dir
    project_new $project_name -overwrite
    set project_opened 1

    set_global_assignment -name FAMILY $family
    set_global_assignment -name DEVICE $device
    set_global_assignment -name TOP_LEVEL_ENTITY $top
    set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files
    set_global_assignment -name NUM_PARALLEL_PROCESSORS ALL
    set_global_assignment -name QIP_FILE $qip_file

    # 只运行 Analysis & Synthesis；独立组件没有板卡 pin 和 SDC，不能生成 .sof。
    execute_module -tool map
} message options]

if {$project_opened && [is_project_open]} {
    project_close
}
cd $original_dir

if {$failed} {
    puts stderr $message
    if {[dict exists $options -errorinfo]} {
        puts stderr [dict get $options -errorinfo]
    }
    exit 1
}

puts "Quartus Analysis & Synthesis passed for $top on $device"
