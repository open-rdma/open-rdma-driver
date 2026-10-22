#!/usr/bin/env bluetcl

package require utils

proc usage {} {
    puts ""
    puts "usage: $::argv0 <options> top_package_name top_module"
    puts "Options:"
    puts "  -q             Do not print section headers"
    puts "  -p <path>      Bluespec search path"
    puts "  -bdir <dir>    Bluespec bdir directory"
    puts "  -vdir <dir>    Bluespec vdir directory"
    puts "  -generated     Print synthesized BSV modules"
    puts "  -primitives    Print Bluespec primitive modules"
    puts "  -imported      Print imported modules"
    puts "  -no-inline-fns Print modules for no-inline functions"
    puts "  -all           Print every Verilog dependency"
    puts ""
}

set boolOptions [list -- -q -generated -primitives -imported -no-inline-fns -all]
set valOptions [list -p -bdir -vdir]

if {[catch [list ::utils::scanOptions $boolOptions $valOptions true OPT "$argv"] opts]} {
    puts stderr $opts
    usage
    exit 1
}

if {[llength $opts] != 2} {
    puts stderr "A top package name and a top module name are required"
    usage
    exit 1
}

if {[info exists OPT(-p)]} {
    Bluetcl::flags set -p $OPT(-p)
}
if {[info exists OPT(-bdir)]} {
    Bluetcl::flags set -bdir $OPT(-bdir)
}
if {[info exists OPT(-vdir)]} {
    Bluetcl::flags set -vdir $OPT(-vdir)
}

if {![info exists OPT(-all)] && ![info exists OPT(-generated)] &&
    ![info exists OPT(-no-inline-fns)] && ![info exists OPT(-primitives)] &&
    ![info exists OPT(-imported)]} {
    set OPT(-all) 1
}

set top_pkg [lindex $opts 0]
Bluetcl::flags set -verilog
Bluetcl::module load $top_pkg

set mods_to_process [list $top_pkg]
set already_done [list]
set mod_info [dict create]

while {[llength $mods_to_process] > 0} {
    set this_mod [utils::head $mods_to_process]
    set mods_to_process [utils::tail $mods_to_process]
    set res [Bluetcl::module submods $this_mod]
    set this_mod_type [lindex $res 0]
    dict set mod_info $this_mod $this_mod_type
    lappend already_done $this_mod

    foreach mod [lindex $res 1] {
        set sub_mod [utils::snd $mod]
        if {[lsearch -exact $already_done $sub_mod] == -1 &&
            [lsearch -exact $mods_to_process $sub_mod] == -1} {
            lappend mods_to_process $sub_mod
        }
    }

    foreach mod [lindex $res 2] {
        set sub_mod [utils::snd $mod]
        if {[lsearch -exact $already_done $sub_mod] == -1 &&
            [lsearch -exact $mods_to_process $sub_mod] == -1} {
            dict set mod_info $sub_mod no-inline-fn
            lappend mods_to_process $sub_mod
        }
    }
}

proc lookupfile {name paths extensions} {
    foreach dir $paths {
        foreach extension $extensions {
            set path [file join $dir "$name.$extension"]
            if {[file exists $path]} {
                return [file normalize $path]
            }
        }
    }
    return "<unable to locate file for module $name>"
}

proc addfile {path list_name} {
    upvar 1 $list_name paths
    if {[lsearch -exact $paths $path] == -1} {
        lappend paths $path
    }
}

set generated [list]
set no_inline [list]
set primitives [list]
set imported [list]
set vdir [lindex [Bluetcl::flags show vdir] 1]
set bluespec_dir $::env(BLUESPECDIR)
set libraries [list [file join $bluespec_dir Verilog] [file join $bluespec_dir Libraries]]
set source_path [split [lindex [Bluetcl::flags show p] 1] ":"]
set generated_path [concat $vdir $libraries]
set imported_path [concat $source_path $libraries]

foreach module_name [dict keys $mod_info] {
    set module_type [dict get $mod_info $module_name]
    if {$module_type == "primitive" && $module_name == "Probe"} {
        continue
    }

    switch -exact $module_type {
        "user" {
            addfile [lookupfile $module_name $generated_path {v}] generated
        }
        "no-inline-fn" {
            addfile [lookupfile $module_name $vdir {v}] no_inline
        }
        "primitive" {
            addfile [lookupfile $module_name $libraries {v}] primitives
        }
        "import" {
            addfile [lookupfile $module_name $imported_path {v vhd vhdl}] imported
        }
    }

    if {$module_type == "primitive"} {
        switch -exact $module_name {
            "MakeReset" {
                addfile [lookupfile SyncReset $libraries {v}] primitives
            }
            "MakeResetA" {
                addfile [lookupfile SyncResetA $libraries {v}] primitives
            }
            "SyncFIFOLevel" {
                addfile [lookupfile ClockGen $libraries {v}] primitives
                addfile [lookupfile SyncHandshake $libraries {v}] primitives
            }
            "SyncFIFO" {
                addfile [lookupfile ClockGen $libraries {v}] primitives
            }
            "SyncRegister" {
                addfile [lookupfile ClockGen $libraries {v}] primitives
                addfile [lookupfile SyncHandshake $libraries {v}] primitives
            }
        }
    }
}

proc print_section {title paths enabled quiet} {
    if {!$enabled || [llength $paths] == 0} {
        return
    }
    if {!$quiet} {
        puts "# $title:"
    }
    foreach path $paths {
        puts $path
    }
}

set quiet [info exists OPT(-q)]
set print_all [info exists OPT(-all)]
print_section "Synthesized user modules" $generated \
    [expr {$print_all || [info exists OPT(-generated)]}] $quiet
print_section "No-inlined functions" $no_inline \
    [expr {$print_all || [info exists OPT(-no-inline-fns)]}] $quiet
print_section "Imported modules" $imported \
    [expr {$print_all || [info exists OPT(-imported)]}] $quiet
print_section "Bluespec library primitives" $primitives \
    [expr {$print_all || [info exists OPT(-primitives)]}] $quiet
