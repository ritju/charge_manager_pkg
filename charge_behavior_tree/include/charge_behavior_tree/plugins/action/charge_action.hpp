#ifndef CHARGE_BEHAVIOR_TREE__PLUGINS__ACTION__CHARGE_ACTION_HPP_
#define CHARGE_BEHAVIOR_TREE__PLUGINS__ACTION__CHARGE_ACTION_HPP_

#include <string>

#include "capella_ros_service_interfaces/msg/charge_state.hpp"
#include "charge_manager_msgs/msg/charge_state2.hpp"
#include "charge_manager_msgs/action/charge.hpp"

#include "nav2_behavior_tree/bt_action_node.hpp"

namespace charge_behavior_tree
{

/**
 * @class ChargeAction
 * @brief A charge_behavior_tree::BtActionNode class that wraps charge_manager_msgs::action::Charge
*/
class ChargeAction : public nav2_behavior_tree::BtActionNode<charge_manager_msgs::action::Charge>
{
public:
/**
 * @brief A constructor for charge_behavior_tree::ChargeAction
 * @param xml_tag_name Name for the XML tag for this node
 * @param action_name Action name this node creates a client for
 * @param conf BT node configuration
*/
ChargeAction(
        const std::string & xml_tag_name,
        const std::string & action_name,
        const BT::NodeConfiguration & conf);

/**
 * @brief Function to perform some user-defined operation on tick
*/
void on_tick() override;

/**
 * @brief Creates list of BT ports
 * @return BT::PortsList Containing basic ports along with node-specific ports
*/
static BT::PortsList providedPorts()
{
        return providedBasicPorts(
                {
                        BT::OutputPort<capella_ros_service_interfaces::msg::ChargeState>("state", "state infomations"),
                        BT::InputPort<std::string>("mac", "the mac address of bluetooth"),
                        BT::InputPort<std::string>("behavior_tree", "Behavior tree to run"),
                }
        );
}

};

} // end of namespace

#endif